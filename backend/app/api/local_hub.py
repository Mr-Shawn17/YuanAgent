"""LocalTaskHub：中心服务侧的本地执行器任务路由。

职责：
- 维护 user_id → WebSocket 连接 的注册表（同用户新连接顶掉旧连接）；
- 每条连接携带其可服务的 workspace 集合（执行器握手后上报）；
- dispatch()：生成 request_id，按 (user_id, workspace) 找连接，发工具任务，等结果；
- list_workspaces()：返回该用户在线执行器上报的可用目录（网页下拉用）；
- 处理 结果回传 / 连接断开 / 超时 三种完成路径，Future 全部有唯一结果；
- 无在线连接时返回可读错误文本（工具错误边界兜底，不炸 Agent 图）。

单实例内存态设计：当前部署形态为单后端进程，不引入 Redis。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# dispatch 全流程超时（含执行器执行时间），与 SubAgent 默认超时对齐
DEFAULT_DISPATCH_TIMEOUT = 120.0
OFFLINE_MESSAGE = (
    "[local_runner_offline] 本地执行器未连接：请先在电脑上启动 YuanRunner，"
    "再重试本操作（云端聊天与检索不受影响）"
)


@dataclass(slots=True)
class WorkspaceInfo:
    """一个可用的工作目录（执行器上报）。"""

    path: str
    name: str = ""
    kind: str = "custom"


@dataclass(slots=True)
class _Connection:
    """一条已注册的执行器连接。"""

    user_id: str
    device_id: str
    websocket: Any
    # 该连接可服务的工作目录（含默认目录 + 扫描到的常用目录）
    workspaces: list[WorkspaceInfo] = field(default_factory=list)
    pending: set[str] = field(default_factory=set)


class LocalTaskHub:
    """进程级任务路由器：工具调用 → 执行器 → 结果。"""

    def __init__(self, *, dispatch_timeout: float = DEFAULT_DISPATCH_TIMEOUT) -> None:
        self._connections: dict[str, _Connection] = {}
        self._futures: dict[str, asyncio.Future[str]] = {}
        self._dispatch_timeout = dispatch_timeout

    def set_dispatch_timeout(self, seconds: float) -> None:
        """调整下发全流程超时（供装配方用 settings.local_dispatch_timeout 覆盖默认值）。"""
        self._dispatch_timeout = max(1.0, float(seconds))

    def is_online(self, user_id: str) -> bool:
        return user_id in self._connections

    def register(self, *, user_id: str, device_id: str, websocket: Any) -> None:
        """注册连接；同用户已有连接时先关闭旧的（新连接顶替）。"""
        old = self._connections.get(user_id)
        if old is not None:
            self._drop(old, reason="superseded")
        self._connections[user_id] = _Connection(
            user_id=user_id, device_id=device_id, websocket=websocket
        )
        logger.info("runner_registered", user_id=user_id, device_id=device_id)

    def set_workspaces(self, *, user_id: str, workspaces: list[WorkspaceInfo]) -> None:
        """执行器上报可服务目录后更新连接元数据。"""
        conn = self._connections.get(user_id)
        if conn is None:
            return
        conn.workspaces = list(workspaces)
        logger.info("runner_workspaces", user_id=user_id, count=len(conn.workspaces))

    def list_workspaces(self, user_id: str) -> list[WorkspaceInfo]:
        """返回该用户在线执行器上报的目录（去重、保持顺序）。"""
        conn = self._connections.get(user_id)
        if conn is None:
            return []
        seen: set[str] = set()
        result: list[WorkspaceInfo] = []
        for info in conn.workspaces:
            if info.path in seen:
                continue
            seen.add(info.path)
            result.append(info)
        return result

    def unregister(self, *, user_id: str, device_id: str) -> None:
        """断开时移除注册；device_id 不匹配说明已被新连接顶替，不动新连接。"""
        conn = self._connections.get(user_id)
        if conn is not None and conn.device_id == device_id:
            self._drop(conn, reason="disconnected")

    def _drop(self, conn: _Connection, *, reason: str) -> None:
        if self._connections.get(conn.user_id) is conn:
            self._connections.pop(conn.user_id, None)
        for request_id in conn.pending:
            future = self._futures.pop(request_id, None)
            if future is not None and not future.done():
                future.set_result(
                    f"[local_runner_disconnected] 任务执行中连接断开（{reason}），请重试"
                )
        conn.pending.clear()
        logger.info("runner_dropped", user_id=conn.user_id, device_id=conn.device_id, reason=reason)

    async def handle_result(
        self, *, request_id: str, ok: bool, output: str = "", error: str = ""
    ) -> None:
        """收到执行器回传：resolve 对应 Future。"""
        future = self._futures.pop(request_id, None)
        if future is None or future.done():
            return
        if ok:
            future.set_result(output or "(执行器未返回内容)")
        else:
            future.set_result(f"[local_runner_error] {error or '执行器返回失败'}")

    async def dispatch(
        self,
        *,
        user_id: str,
        tool: str,
        args: dict[str, Any],
        workspace: str = "",
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> str:
        """把工具任务发给该用户的执行器并等待结果文本。

        离线 / 未知消息 / 超时 都返回带前缀的错误文本，永不抛异常——
        工具错误边界会把它原样交给模型，让模型向用户解释。
        """
        conn = self._connections.get(user_id)
        if conn is None:
            return OFFLINE_MESSAGE
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._futures[request_id] = future
        conn.pending.add(request_id)
        payload = {
            "type": "tool_request",
            "request_id": request_id,
            "tool": tool,
            "args": args,
            "workspace": workspace,
            "timeout_seconds": timeout or self._dispatch_timeout,
        }
        try:
            await conn.websocket.send_json(payload)
        except Exception as exc:
            self._futures.pop(request_id, None)
            conn.pending.discard(request_id)
            self._drop(conn, reason="send_failed")
            return f"[local_runner_send_failed] 下发任务失败：{exc}"[:200]
        try:
            return await asyncio.wait_for(future, timeout=timeout or self._dispatch_timeout)
        except TimeoutError:
            return (
                f"[local_runner_timeout] 执行器 {timeout or self._dispatch_timeout:.0f}s "
                "内未返回结果，任务可能仍在执行或已丢失"
            )
        finally:
            self._futures.pop(request_id, None)
            conn.pending.discard(request_id)

    async def aclose(self) -> None:
        """关停：resolve 所有在途 Future，避免悬挂。"""
        for request_id, future in list(self._futures.items()):
            if not future.done():
                future.set_result("[local_runner_closed] 服务关停，任务中止")
            self._futures.pop(request_id, None)
        for conn in list(self._connections.values()):
            self._drop(conn, reason="server_shutdown")
        self._connections.clear()


__all__ = [
    "DEFAULT_DISPATCH_TIMEOUT",
    "OFFLINE_MESSAGE",
    "LocalTaskHub",
    "WorkspaceInfo",
]
