"""GET /v1/sessions：会话列表（与 CLI /sessions 共用 builder.list_sessions）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.agents.builder import list_sessions
from app.api.deps import get_runtime
from app.api.runtime import AgentRuntime
from app.api.schemas import SessionInfo

router = APIRouter()


@router.get("/v1/sessions")
async def sessions(
    limit: int = 50,
    runtime: AgentRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    """按最近更新倒序列出历史会话（thread_id/标题/消息数/更新时间）。"""
    rows = await list_sessions(runtime.checkpointer, limit=max(1, min(limit, 400)))
    return {"sessions": [SessionInfo(**row).model_dump() for row in rows]}


__all__ = ["router"]
