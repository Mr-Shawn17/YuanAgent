from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from langchain.agents.middleware.types import AgentState as BaseAgentState


class Citation(TypedDict):
    index: int
    doc: str
    chunk_id: str
    locator: str
    snippet: str
    score: float


class RetrievedChunk(TypedDict):
    chunk_id: str
    text: str
    source: str
    score: float


class AgentState(BaseAgentState):
    session_id: NotRequired[str]
    user_id: NotRequired[str]
    citations: NotRequired[list[Citation]]
    retrieved: NotRequired[list[RetrievedChunk]]
    # 意图预判层（docs/intent-recognition.md §7）：每轮由 run_intent_gate 写入的
    # 路由指令/强提示，经 IntentNoteMiddleware 注入 system prompt；空串=未启用
    intent_note: NotRequired[str]


def initial_state_fields(
    *,
    session_id: str,
    user_id: str,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "user_id": user_id,
        "citations": [],
        "retrieved": [],
        "intent_note": "",
    }


__all__ = ["AgentState", "Citation", "RetrievedChunk", "initial_state_fields"]
