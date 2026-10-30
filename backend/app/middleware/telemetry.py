"""telemetry 中间件：模型调用观测。

记录每次模型调用的耗时与 token 用量（来自 AIMessage.usage_metadata），
以结构化日志输出，为 P7' 的可观测性（JSON 日志/指标）打底。
只观测不改写：请求与响应原样透传，异常也照常上抛。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage

from app.core.logging import get_logger

logger = get_logger(__name__)


def extract_message(result: Any) -> AIMessage | None:
    """从 handler 返回值里取出 AIMessage（可能是裸消息或 ModelResponse）。"""
    if isinstance(result, AIMessage):
        return result
    inner = getattr(result, "result", None)
    if isinstance(inner, AIMessage):
        return inner
    return None


def usage_of(message: AIMessage | None) -> dict[str, int]:
    """提取 token 用量；缺失时返回空 dict（部分模型/流式场景没有该字段）。"""
    if message is None:
        return {}
    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, dict):
        return {}
    return {str(key): int(value) for key, value in usage.items() if isinstance(value, (int, float))}


class TelemetryMiddleware(AgentMiddleware):
    """给每次模型调用打点：耗时 + token 用量 + 消息规模。"""

    def __init__(self, *, user_id: str = "", session_id: str = "") -> None:
        super().__init__()
        self._user_id = user_id
        self._session_id = session_id

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        started = perf_counter()
        try:
            result = await handler(request)
        except Exception as exc:
            latency_ms = int((perf_counter() - started) * 1000)
            logger.warning(
                "model_call_failed",
                user_id=self._user_id,
                session_id=self._session_id,
                latency_ms=latency_ms,
                error=exc.__class__.__name__,
            )
            raise
        latency_ms = int((perf_counter() - started) * 1000)
        message = extract_message(result)
        logger.info(
            "model_call",
            user_id=self._user_id,
            session_id=self._session_id,
            latency_ms=latency_ms,
            messages=len(request.messages),
            tools=len(request.tools or []),
            has_tool_calls=bool(message is not None and message.tool_calls),
            **usage_of(message),
        )
        return result


__all__ = ["TelemetryMiddleware", "extract_message", "usage_of"]
