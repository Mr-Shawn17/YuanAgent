"""记忆召回中间件：每次模型调用前把长期记忆注入 system prompt。

对应计划 §P4'「召回用关键词+过滤（无语义，R3）」：
- 从最近一条用户消息提取查询词，调 app.memory.recall.recall_memories；
- 召回结果用 prompts/memory.j2 渲染后追加到 system_message 末尾
  （ModelRequest.override 返回新请求对象，不改动原状态）；
- 召回失败只记日志，绝不让记忆系统炸掉对话主链路。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import HumanMessage
from langgraph.store.base import BaseStore

from app.agents.builder import append_system_fragment, render_prompt
from app.core.events import EventBus, MemoryRecalledEvent
from app.core.logging import get_logger
from app.memory.recall import DEFAULT_RECALL_LIMIT, RecalledMemory, recall_memories

logger = get_logger(__name__)


def latest_human_text(messages: list[Any]) -> str:
    """取最近一条用户消息的纯文本，作为本轮召回的查询词。"""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            if isinstance(content, str):
                return content
            return str(content)
    return ""


class MemoryRecallMiddleware(AgentMiddleware):
    """召回长期记忆并注入 system prompt 的中间件。"""

    def __init__(
        self,
        *,
        store: BaseStore,
        user_id: str,
        event_bus: EventBus | None = None,
        limit: int = DEFAULT_RECALL_LIMIT,
    ) -> None:
        super().__init__()
        self._store = store
        self._user_id = user_id
        self._event_bus = event_bus
        self._limit = limit

    async def _recall(self, query: str) -> list[RecalledMemory]:
        try:
            return await recall_memories(self._store, self._user_id, query, limit=self._limit)
        except Exception:
            logger.warning("memory_recall_failed", user_id=self._user_id, exc_info=True)
            return []

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        query = latest_human_text(list(request.messages))
        memories = await self._recall(query) if query.strip() else []
        if memories:
            fragment = render_prompt("memory.j2", memories=memories)
            request = append_system_fragment(request, fragment)
            if self._event_bus is not None:
                await self._event_bus.publish(
                    MemoryRecalledEvent(
                        user_id=self._user_id,
                        count=len(memories),
                        items=[
                            {"kind": item.kind, "preview": item.preview, "score": item.score}
                            for item in memories
                        ],
                    )
                )
        return await handler(request)


__all__ = ["MemoryRecallMiddleware", "latest_human_text"]
