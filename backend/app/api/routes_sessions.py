"""会话与工作目录相关端点。

- GET /v1/sessions：会话列表（与 CLI /sessions 共用 builder.list_sessions）
- GET /v1/workspaces：在线执行器上报的可用工作目录（网页下拉用）
- PUT /v1/sessions/{id}/workspace：绑定/更新会话的工作目录
- GET /v1/sessions/{id}/workspace：读取会话当前绑定的工作目录
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.agents.builder import list_sessions
from app.api.deps import get_runtime
from app.api.local_hub import WorkspaceInfo
from app.api.runtime import AgentRuntime
from app.api.schemas import SessionInfo, WorkspaceBindRequest
from app.core.errors import NotFoundError
from app.db.session_repo import SessionRepo

router = APIRouter()

# sessions.meta 里存工作目录的键名
WORKSPACE_META_KEY = "workspace"


@router.get("/v1/sessions")
async def sessions(
    limit: int = 50,
    runtime: AgentRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    """按最近更新倒序列出历史会话（thread_id/标题/消息数/更新时间/工作目录）。"""
    rows = await list_sessions(runtime.checkpointer, limit=max(1, min(limit, 400)))
    items = []
    for row in rows:
        info = SessionInfo(**row).model_dump()
        info["workspace"] = _workspace_of(row)
        items.append(info)
    return {"sessions": items}


@router.get("/v1/workspaces")
async def workspaces(
    runtime: AgentRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    """返回在线执行器上报的可用工作目录列表（去重、保持顺序）。"""
    items: list[WorkspaceInfo] = runtime.local_hub.list_workspaces(runtime.user_id)
    return {
        "workspaces": [{"path": w.path, "name": w.name, "kind": w.kind} for w in items]
    }


@router.get("/v1/sessions/{session_id}/workspace")
async def get_session_workspace(
    session_id: str,
    runtime: AgentRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    """读取会话当前绑定的工作目录（未绑定返回空串）。"""
    workspace = await _load_workspace(runtime, session_id)
    return {"session_id": session_id, "workspace": workspace}


@router.put("/v1/sessions/{session_id}/workspace")
async def set_session_workspace(
    session_id: str,
    payload: WorkspaceBindRequest,
    runtime: AgentRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    """绑定/更新会话的工作目录（持久化到 sessions.meta）。"""
    workspace = payload.workspace.strip()
    if not workspace:
        raise NotFoundError("workspace 不能为空")
    await _save_workspace(runtime, session_id, workspace)
    return {"session_id": session_id, "workspace": workspace}


def _workspace_of(row: Any) -> str:
    """从会话行读取绑定的工作目录。"""
    meta = getattr(row, "meta", None) or {}
    if isinstance(meta, dict):
        return str(meta.get(WORKSPACE_META_KEY, ""))
    return ""


async def _load_workspace(runtime: AgentRuntime, session_id: str) -> str:
    """读会话绑定的工作目录；数据库不可用或会话不存在返回空串。"""
    if runtime.database is None:
        return ""
    try:
        async with runtime.database.session() as session:
            record = await SessionRepo(session).get_session(session_id)
            if record is None:
                return ""
            return _workspace_of(record)
    except Exception:
        return ""


async def _save_workspace(runtime: AgentRuntime, session_id: str, workspace: str) -> None:
    """写会话绑定的工作目录；数据库不可用时静默跳过（不阻断对话）。"""
    if runtime.database is None:
        return
    async with runtime.database.session() as session:
        repo = SessionRepo(session)
        await repo.set_meta(session_id, WORKSPACE_META_KEY, workspace, user_id=runtime.user_id)


__all__ = ["WORKSPACE_META_KEY", "router"]
