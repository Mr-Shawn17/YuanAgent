"""API 层的 Pydantic 请求/响应模型（计划 §6 api/schemas.py）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """POST /v1/chat 请求体：message 为本轮用户输入，session_id 留空则新建会话。"""

    message: str = ""
    session_id: str = ""
    user_id: str = ""


class ApproveRequest(BaseModel):
    """POST /v1/chat/{run_id}/approve 请求体（v1 计划 §6.3 审批恢复协议）。"""

    call_id: str = ""
    decision: Literal["allow", "deny"]


class SessionInfo(BaseModel):
    """GET /v1/sessions 的单条会话摘要。"""

    thread_id: str
    updated_at: str
    message_count: int
    title: str
    workspace: str = ""


class WorkspaceBindRequest(BaseModel):
    """PUT /v1/sessions/{id}/workspace 请求体。"""

    workspace: str


class SessionMessage(BaseModel):
    """会话历史回放消息（user/assistant 两角色；assistant 可携带执行链路图谱）。"""

    role: str
    content: str
    graph: dict[str, Any] | None = None


class OpenAIMessage(BaseModel):
    """OpenAI 兼容消息：content 允许 str 或多模态块列表（只提取 text 块）。"""

    role: str
    content: Any = ""


class OpenAIChatRequest(BaseModel):
    """POST /v1/chat/completions 请求体（OpenAI 兼容 + session_id 扩展字段）。"""

    model: str = ""
    messages: list[OpenAIMessage] = Field(default_factory=list)
    stream: bool = False
    session_id: str = ""
    user: str = ""


__all__ = [
    "ApproveRequest",
    "ChatRequest",
    "OpenAIChatRequest",
    "OpenAIMessage",
    "SessionInfo",
    "SessionMessage",
    "WorkspaceBindRequest",
]
