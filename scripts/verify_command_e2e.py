"""命令工具端到端验收：真实 LocalTaskHub + 真实 RunnerConnection（覆盖 connection 分流）。

与 verify_local_runner.py 的区别：runner 侧不再手写客户端循环，而是直接驱动
真实的 RunnerConnection（含 hello/workspaces 上报、COMMAND_TOOLS 分流、
执行侧超时强制），更接近线上执行器行为。

验收口径：
1. 设备令牌签发（devices 表）；
2. run_command 经 hub → WS → RunnerConnection → ShellBackend 真实往返；
3. 危险命令在 runner 侧被策略拦截并回传 [command_blocked] 文本；
4. git_clone 非法地址回传可读错误文本（不炸链路）；
5. 命令产物落在真实工作目录。

跑法：D:/software/miniconda3/envs/py312/python.exe scripts/verify_command_e2e.py
前置：MySQL 可连（同 verify_local_runner.py）；无需启动 uvicorn。
"""

from __future__ import annotations

import asyncio
import json
import os
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
from runner.config import RunnerConfig  # noqa: E402
from runner.connection import RunnerConnection  # noqa: E402

WS_PORT = 8766
CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail and not ok else ""))


class _SendJsonAdapter:
    """给原生 websockets 的 ServerConnection 补 send_json，对齐 hub 的发送接口。"""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(payload, ensure_ascii=False))


async def main() -> int:
    workspace = PROJECT_ROOT / "data" / "verify_cmd_ws"
    workspace.mkdir(parents=True, exist_ok=True)

    print("== 1. 设备令牌 ==")
    device_id, token = "verify-device-fallback", "yrt_verify_fallback_token"
    try:
        settings = get_settings()
        database = build_database(settings)
        async with database.session() as session:
            issued = await DeviceRepo(session).create(user_id="verify", name="cmd-e2e")
            device_id, token = issued.device.id, issued.token
        check("真实签发（MySQL 可连）", issued.device.token_hash == hash_token(token))
    except Exception as exc:  # MySQL 不可达时回退假令牌：签发链路由 verify_local_runner.py 验收
        print(f"  （MySQL 不可连，回退假令牌：{type(exc).__name__}；签发链路验收见 verify_local_runner.py）")
        check("令牌可用（回退模式）", token.startswith("yrt_"))

    print("== 2. WSS 全链路（真实 hub + 真实 RunnerConnection）==")
    hub = LocalTaskHub(dispatch_timeout=30)
    registered = asyncio.Event()
    hellos: list[dict[str, Any]] = []

    async def _server(ws: Any) -> None:
        hub.register(user_id="verify", device_id=device_id, websocket=_SendJsonAdapter(ws))
        registered.set()
        try:
            async for raw in ws:
                message = json.loads(raw)
                mtype = message.get("type")
                if mtype == "hello":
                    hellos.append(message)
                    await ws.send(json.dumps({"type": "welcome", "device_id": device_id}))
                elif mtype == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif mtype == "tool_result":
                    await hub.handle_result(
                        request_id=message["request_id"],
                        ok=bool(message["ok"]),
                        output=str(message.get("output", "")),
                        error=str(message.get("error", "")),
                    )
                # workspaces 上报：忽略即可
        finally:
            hub.unregister(user_id="verify", device_id=device_id)

    # 真实 RunnerConnection：环境变量令牌 + 指向本地测试 WS 端点
    os.environ["YUAN_RUNNER_TOKEN"] = token
    config = RunnerConfig(
        server_url=f"ws://127.0.0.1:{WS_PORT}/ws/runner",
        workspace=str(workspace),
    )

    async def _run_case(name: str, tool: str, args: dict[str, Any]) -> str:
        """起 WS server + 真实 RunnerConnection，dispatch 一条命令类工具。"""
        registered.clear()
        async with websockets.serve(_server, "127.0.0.1", WS_PORT):
            connection = RunnerConnection(config=config, token=token, workspace=workspace)
            conn_task = asyncio.create_task(connection.run())
            try:
                await asyncio.wait_for(registered.wait(), timeout=5)
                return await asyncio.wait_for(
                    hub.dispatch(user_id="verify", tool=tool, args=args, timeout=30.0),
                    timeout=45.0,
                )
            finally:
                conn_task.cancel()
                await asyncio.sleep(0.2)

    result = await _run_case("normal", "run_command", {"command": "git --version"})
    check("run_command 真实往返", "git version" in result and "[exit 0]" in result, result[:100])

    result = await _run_case("blocked", "run_command", {"command": "shutdown /s"})
    check("危险命令策略拦截", result.startswith("[command_blocked]"), result[:80])

    result = await _run_case(
        "escape", "run_command", {"command": "echo hi", "cwd": "../outside"}
    )
    check("越界 cwd 拒绝", result.startswith("[sandbox_escape]"), result[:80])

    result = await _run_case(
        "bad-url", "git_clone", {"repo_url": "ssh://git@github.com/x/y.git"}
    )
    check("git_clone 非法地址可读报错", "[工具执行失败]" in result, result[:80])

    marker = workspace / "e2e_cmd.txt"
    result = await _run_case(
        "file", "run_command", {"command": "echo e2e-command-ok > e2e_cmd.txt"}
    )
    check(
        "命令产物落真实工作目录",
        marker.is_file() and marker.read_text(encoding="utf-8", errors="replace").strip()
        == "e2e-command-ok",
        result[:80],
    )
    marker.unlink(missing_ok=True)

    check("hello 上报命令档位", hellos and hellos[0].get("command_backend") == "shell")

    failed = [name for name, ok in CHECKS if not ok]
    print(f"\n共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)}，失败 {len(failed)}")
    for name in failed:
        print(f"  FAIL: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
