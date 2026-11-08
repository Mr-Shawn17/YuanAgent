"""WSS 客户端：鉴权连接 → hello 注册 → 心跳 → 收任务执行并回传 → 断线指数退避重连。

request_id 幂等：执行前查缓存，命中直接回传缓存结果（防断线重发重复执行写操作）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

import websockets

from runner import __version__
from runner.auth import get_token
from runner.config import RunnerConfig
from runner.executor import execute_tool
from runner.protocol import ToolRequest, WorkspaceInfo, WorkspacesMessage
from runner.scanner import scan_workspaces

logger = logging.getLogger("runner.connection")

CLIENT_ID = f"yuan-runner/{__version__}"
RESULT_CACHE_LIMIT = 256


class ResultCache:
    """request_id → 结果文本 的 LRU（幂等重放保护）。"""

    def __init__(self, limit: int = RESULT_CACHE_LIMIT) -> None:
        self._limit = limit
        self._data: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: str, value: str) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._limit:
            self._data.popitem(last=False)


class RunnerConnection:
    """单次连接会话：内部 asyncio 任务组（收消息 + 心跳）。"""

    def __init__(self, *, config: RunnerConfig, token: str, workspace: Path) -> None:
        self._config = config
        self._token = token
        self._workspace = workspace
        self._cache = ResultCache()
        # 心跳：每 interval 发 ping 且 missed+1；收到 pong 清零；连续 3 次未清零断开
        self._missed_pongs = 0
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        """连接并服务，直到断开（由外层决定是否重连）。"""
        headers = [("Authorization", f"Bearer {self._token}")]
        async with websockets.connect(
            self._config.server_url,
            additional_headers=headers,
            max_size=16 * 1024 * 1024,
            ping_interval=None,  # 心跳自管（应用层 ping/pong）
        ) as ws:
            await ws.send(json.dumps({"type": "hello", "client": CLIENT_ID}))
            # 握手后上报本机可用工作目录列表（网页下拉用）
            await self._send_workspaces(ws)
            stop = asyncio.Event()
            tasks = [
                asyncio.create_task(self._receive_loop(ws)),
                asyncio.create_task(self._heartbeat_loop(ws, stop)),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                for task in self._tasks:
                    task.cancel()

    async def _send_workspaces(self, ws: Any) -> None:
        """上报本机可用工作目录（默认 workspace 置顶，其余为常用目录）。"""
        workspaces: list[WorkspaceInfo] = []
        default = str(self._workspace)
        workspaces.append(WorkspaceInfo(path=default, name="当前工作目录", kind="custom"))
        for info in scan_workspaces():
            if info.path != default:
                workspaces.append(info)
        try:
            await ws.send(
                json.dumps(
                    WorkspacesMessage(workspaces=workspaces).model_dump(),
                    ensure_ascii=False,
                )
            )
        except Exception:
            logger.warning("send_workspaces_failed")

    async def _receive_loop(self, ws: Any) -> None:
        try:
            async for raw in ws:
                await self._handle_message(ws, raw)
        except Exception as exc:
            logger.warning("receive_loop_ended: %s", exc)
            raise

    async def _heartbeat_loop(self, ws: Any, stop: asyncio.Event) -> None:
        interval = self._config.heartbeat_seconds
        while not stop.is_set():
            await asyncio.sleep(interval)
            if self._missed_pongs >= 3:
                logger.warning("heartbeat_timeout（3 次未收 pong），主动断开")
                await ws.close()
                return
            try:
                await ws.send(json.dumps({"type": "ping"}))
                self._missed_pongs += 1
            except Exception:
                return

    async def _handle_message(self, ws: Any, raw: str | bytes) -> None:
        try:
            message: dict[str, Any] = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("invalid_json dropped")
            return
        mtype = str(message.get("type", ""))
        if mtype == "pong":
            self._missed_pongs = 0
            return
        if mtype == "welcome":
            logger.info("registered_by_server: %s", message.get("device_id", ""))
            return
        if mtype == "tool_request":
            request = ToolRequest.model_validate(message)
            task = asyncio.create_task(self._execute_and_reply(ws, request))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        if mtype == "cancel":
            logger.info("cancel received（当前任务不支持中断，仅记录）: %s", message)
            return
        logger.warning("unknown_message_type: %s", mtype)

    async def _execute_and_reply(self, ws: Any, request: ToolRequest) -> None:
        cached = self._cache.get(request.request_id)
        if cached is not None:
            logger.info("replayed_request: %s", request.request_id)
            await self._send_result(ws, request.request_id, cached)
            return
        try:
            # 工具请求可指定 workspace（会话绑定目录）；为空回退连接默认目录。
            # 目标目录由执行器侧做沙箱兜底校验（_prepare_workspace 确保存在）。
            workspace = self._resolve_workspace(request.workspace)
            output = await asyncio.to_thread(
                execute_tool, workspace, request.tool, dict(request.args), self._config
            )
            self._cache.put(request.request_id, output)
            logger.info(
                "tool_done tool=%s workspace=%s request=%s output_len=%d",
                request.tool,
                workspace,
                request.request_id,
                len(output),
            )
            await self._send_result(ws, request.request_id, output)
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning("tool_failed tool=%s error=%s", request.tool, error_text)
            await self._send_result(
                ws, request.request_id, f"（执行失败）{error_text}", ok=False
            )

    def _resolve_workspace(self, raw: str) -> Path:
        """解析工具请求的 workspace；空或不存在时回退连接默认目录。"""
        if not raw.strip():
            return self._workspace
        candidate = Path(raw).expanduser().resolve()
        if not candidate.is_dir():
            logger.warning("workspace_missing fallback=%s", self._workspace)
            return self._workspace
        return candidate

    async def _send_result(
        self, ws: Any, request_id: str, output: str, *, ok: bool = True
    ) -> None:
        payload = {
            "type": "tool_result",
            "request_id": request_id,
            "ok": ok,
            "output": output if ok else "",
            "error": "" if ok else output,
        }
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:
            logger.warning("send_result_failed: %s", request_id)


def _prepare_workspace(raw: str) -> Path:
    """展开 ~ 并确保工作目录存在（阻塞 IO，放线程里跑）。"""
    workspace = Path(raw).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


async def run_forever(config: RunnerConfig) -> None:
    """主循环：连接 → 断开 → 退避重连，永不主动退出（Ctrl+C / 进程终止为止）。"""
    token = get_token()
    if not token:
        logger.error("no_device_token：请先执行 python -m runner set-token <令牌>")
        return
    workspace = await asyncio.to_thread(_prepare_workspace, config.workspace)
    logger.info("runner_starting server=%s workspace=%s", config.server_url, workspace)

    delay = config.reconnect_initial
    while True:
        try:
            connection = RunnerConnection(
                config=config, token=token, workspace=workspace
            )
            logger.info("connected")
            await connection.run()
            logger.info("disconnected")
            delay = config.reconnect_initial  # 成功连过则重置退避
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("connection_failed: %s", exc)
        logger.info("reconnect_in %.1fs", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, config.reconnect_max)


__all__ = ["CLIENT_ID", "ResultCache", "RunnerConnection", "run_forever"]
