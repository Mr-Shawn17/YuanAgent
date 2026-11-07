"""/ws/runner：本地执行器的 WebSocket 接入端点。

鉴权：Authorization: Bearer <设备令牌>（devices 表校验，吊销即失效）。
消息：hello（注册）→ ping/pong 心跳 → tool_request/tool_result 双向任务流。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.local_hub import WorkspaceInfo
from app.api.runtime import AgentRuntime
from app.core.logging import get_logger
from app.db.device_repo import DeviceRepo
from app.db.engine import Database

router = APIRouter()
logger = get_logger(__name__)

# 心跳：服务端每 30s 下发 ping 帧由客户端主动 ping 的对称实现——
# 实际采用客户端 ping / 服务端 pong，这里只定义服务端容忍度。
HEARTBEAT_INTERVAL = 30.0


def hub_workspace_from_dict(item: dict[str, Any]) -> WorkspaceInfo:
    """执行器上报的 workspaces 条目 → hub 的 WorkspaceInfo。"""
    return WorkspaceInfo(
        path=str(item.get("path", "")),
        name=str(item.get("name", "")),
        kind=str(item.get("kind", "custom")),
    )


def _bearer_token(websocket: WebSocket) -> str:
    """从 Authorization: Bearer 头取令牌；兼容原生浏览器 WebSocket（无自定义头）。"""
    auth = websocket.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # 浏览器 WebSocket 不能带自定义头：允许 ?token= 查询参数兜底
    return str(websocket.query_params.get("token", "")).strip()


async def _auth_device(database: Database | None, token: str) -> tuple[str, str] | None:
    """校验令牌，返回 (user_id, device_id)；无效返回 None。"""
    if not token or database is None:
        return None
    try:
        async with database.session() as session:
            record = await DeviceRepo(session).get_by_token(token)
            if record is None:
                return None
            return record.user_id, record.id
    except Exception:
        logger.warning("runner_auth_db_failed", exc_info=True)
        return None


@router.websocket("/ws/runner")
async def runner_ws(websocket: WebSocket) -> None:
    """执行器接入：鉴权 → hello 注册 → 消息循环 → 断开清理。

    runtime 从 websocket.app.state 取（WebSocket 上下文无法注入 Request 依赖，
    不能用 Depends(get_runtime)）。"""
    runtime: AgentRuntime = websocket.app.state.runtime
    token = _bearer_token(websocket)
    identity = await _auth_device(runtime.database, token)
    if identity is None:
        await websocket.close(code=4401, reason="invalid device token")
        return
    user_id, device_id = identity
    await websocket.accept()
    hub = runtime.local_hub
    background: set[asyncio.Task[None]] = set()

    registered = False
    try:
        while True:
            message: dict[str, Any] = await websocket.receive_json()
            mtype = str(message.get("type", ""))
            if mtype == "hello":
                hub.register(
                    user_id=user_id, device_id=device_id, websocket=websocket
                )
                registered = True
                await websocket.send_json({"type": "welcome", "device_id": device_id})
                task = asyncio.create_task(_touch_seen(runtime.database, device_id))
                background.add(task)
                task.add_done_callback(background.discard)
            elif mtype == "ping":
                await websocket.send_json({"type": "pong"})
            elif mtype == "workspaces":
                workspaces = [
                    hub_workspace_from_dict(item)
                    for item in message.get("workspaces", [])
                    if isinstance(item, dict) and item.get("path")
                ]
                hub.set_workspaces(user_id=user_id, workspaces=workspaces)
            elif mtype == "tool_result":
                request_id = str(message.get("request_id", ""))
                if not request_id:
                    continue
                await hub.handle_result(
                    request_id=request_id,
                    ok=bool(message.get("ok", False)),
                    output=str(message.get("output", "")),
                    error=str(message.get("error", "")),
                )
            else:
                await websocket.send_json(
                    {"type": "error", "message": f"未知消息类型：{mtype}"}
                )
    except WebSocketDisconnect:
        pass
    finally:
        if registered:
            hub.unregister(user_id=user_id, device_id=device_id)
        for task in background:
            task.cancel()


async def _touch_seen(database: Database | None, device_id: str) -> None:
    """异步更新设备活跃时间（去抖在 repo 内），失败静默。"""
    if database is None:
        return
    try:
        async with database.session() as session:
            await DeviceRepo(session).touch_seen(device_id)
    except Exception:
        logger.warning("runner_touch_seen_failed", device_id=device_id, exc_info=True)


__all__ = ["HEARTBEAT_INTERVAL", "router"]
