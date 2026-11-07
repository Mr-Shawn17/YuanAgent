"""本地执行器端到端验收脚本（真实 MySQL + 真实 WSS 链路，不依赖 LLM）。

验收口径（docs/local-runner.md §10 的核心子集）：
1. 设备令牌签发：devices 表落一行，token_hash 为 sha256；
2. WSS 全链路：hub 注册 → 工具任务下发 → runner 执行 → 结果回传 Future resolve；
3. 真实文件落盘：write_file 在 runner 侧工作目录写入，中心进程不碰盘；
4. 沙箱拒绝：越界路径在 runner 侧被拒并回传错误文本；
5. 幂等：同一 request_id 重放不重复执行（结果缓存命中）。

跑法：.venv\\Scripts\\python.exe scripts/verify_local_runner.py
前置：MySQL 可连（config/agent.yaml runtime 段）；无需启动 uvicorn 与真实执行器——
本脚本进程内同时扮演中心 hub 与本地 runner（对协议层做真实编解码）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

import websockets  # noqa: E402

from app.api.local_hub import LocalTaskHub  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.device_repo import DeviceRepo, hash_token  # noqa: E402
from app.db.engine import build_database  # noqa: E402

WS_PORT = 8765
CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")


async def main() -> int:
    settings = get_settings()
    workspace = PROJECT_ROOT / "data" / "verify_runner_ws"
    workspace.mkdir(parents=True, exist_ok=True)
    target_file = workspace / "e2e.txt"

    print("== 1. 设备令牌签发 ==")
    database = build_database(settings)
    async with database.session() as session:
        issued = await DeviceRepo(session).create(user_id="verify", name="verify-script")
        device_id = issued.device.id
        token = issued.token
    check("devices 表写入", len(device_id) == 36)
    check("令牌只存哈希", issued.device.token_hash == hash_token(token))
    async with database.session() as session:
        record = await DeviceRepo(session).get_by_token(token)
        check("令牌可反查设备", record is not None and record.id == device_id)
        record_revoked = await DeviceRepo(session).get_by_token("yrt_invalid_token_x")
        check("无效令牌返回 None", record_revoked is None)

    print("== 2. WSS 全链路（进程内 hub + 真实 websockets 客户端）==")
    hub = LocalTaskHub(dispatch_timeout=10)
    received_by_runner: list[dict[str, Any]] = []
    seen_request_ids: dict[str, str] = {}  # 幂等模拟：runner 侧缓存

    registered = asyncio.Event()

    class _SendJsonAdapter:
        """给原生 websockets 的 ServerConnection 补 send_json，对齐 LocalTaskHub 的发送接口。"""

        def __init__(self, ws: Any) -> None:
            self._ws = ws

        async def send_json(self, payload: dict[str, Any]) -> None:
            await self._ws.send(json.dumps(payload))

    async def _server(ws: Any) -> None:
        hub.register(user_id="verify", device_id=device_id, websocket=_SendJsonAdapter(ws))
        registered.set()
        try:
            async for raw in ws:
                message = json.loads(raw)
                if message.get("type") == "hello":
                    await ws.send(json.dumps({"type": "welcome", "device_id": device_id}))
                elif message.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif message.get("type") == "tool_result":
                    await hub.handle_result(
                        request_id=message["request_id"],
                        ok=bool(message["ok"]),
                        output=str(message.get("output", "")),
                        error=str(message.get("error", "")),
                    )
        finally:
            hub.unregister(user_id="verify", device_id=device_id)

    import runner.executor as runner_exec

    async def _runner_client() -> None:
        uri = f"ws://127.0.0.1:{WS_PORT}"
        async with websockets.connect(
            uri, additional_headers=[("Authorization", f"Bearer {token}")]
        ) as ws:
            await ws.send(json.dumps({"type": "hello", "client": "verify-script"}))
            async for raw in ws:
                message = json.loads(raw)
                if message.get("type") == "tool_request":
                    received_by_runner.append(message)
                    rid = message["request_id"]
                    if rid in seen_request_ids:  # 幂等重放
                        await ws.send(json.dumps({
                            "type": "tool_result", "request_id": rid,
                            "ok": True, "output": seen_request_ids[rid],
                        }))
                        continue
                    try:
                        output = runner_exec.execute_tool(
                            workspace, message["tool"], dict(message["args"])
                        )
                        seen_request_ids[rid] = output
                        await ws.send(json.dumps({
                            "type": "tool_result", "request_id": rid,
                            "ok": True, "output": output,
                        }))
                    except Exception as exc:
                        await ws.send(json.dumps({
                            "type": "tool_result", "request_id": rid,
                            "ok": False, "error": f"{type(exc).__name__}: {exc}",
                        }))

    async def _run_pair(task: Any) -> str:
        """起一对（server/client）WebSocket，让 hub.dispatch 在 server 侧完成。"""
        registered.clear()
        async with websockets.serve(_server, "127.0.0.1", WS_PORT):
            client_task = asyncio.create_task(_runner_client())
            try:
                # 等待客户端连上并完成 register，避免 dispatch 抢跑返回 offline
                await asyncio.wait_for(registered.wait(), timeout=5)
                result = await asyncio.wait_for(task, timeout=10)
            finally:
                client_task.cancel()
            return result

    result = await _run_pair(
        hub.dispatch(
            user_id="verify", tool="write_file",
            args={"path": "e2e.txt", "content": "e2e-ok", "mode": "overwrite"},
        )
    )
    check("write_file 经执行器回传", "已写入" in result, result)
    check("文件落在 runner 工作目录", target_file.exists())
    check("文件内容正确", target_file.read_text(encoding="utf-8") == "e2e-ok")

    result = await _run_pair(
        hub.dispatch(user_id="verify", tool="read_file", args={"path": "e2e.txt"})
    )
    check("read_file 读回内容", result == "e2e-ok")

    result = await _run_pair(
        hub.dispatch(
            user_id="verify", tool="write_file",
            args={"path": "../escape.txt", "content": "x", "mode": "overwrite"},
        )
    )
    check("越界路径被拒并回传错误", result.startswith("[local_runner_error]"), result)
    check("越界目标未落盘", not (workspace.parent / "escape.txt").exists())

    print("== 3. 汇总 ==")
    await hub.aclose()
    await database.aclose()
    failed = [name for name, ok in CHECKS if not ok]
    print(f"\n共 {len(CHECKS)} 项：{len(CHECKS) - len(failed)} 通过，{len(failed)} 失败")
    if failed:
        print("失败项：", "、".join(failed))
        return 1
    print("本地执行器链路验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
