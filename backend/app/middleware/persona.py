"""persona 中间件：长对话防人设漂移。

system.j2 每轮都会注入完整人设，但对话被摘要压缩或拉得很长后，
模型仍可能逐渐偏离说话方式与边界。这里在消息数超过阈值后，
于 system prompt 末尾追加一段简短的人设强化提醒。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest

from app.agents.builder import append_system_fragment, render_prompt

# 消息数超过该阈值后才开始强化提醒（短对话没有漂移风险，省 token）
DEFAULT_REMIND_AFTER = 20


class PersonaMiddleware(AgentMiddleware):
    """对话变长后在 system prompt 末尾追加人设强化提醒。"""

    def __init__(
        self,
        *,
        persona_name: str,
        persona_role: str = "",
        persona_style: str = "",
        persona_boundaries: str = "",
        remind_after: int = DEFAULT_REMIND_AFTER,
    ) -> None:
        super().__init__()
        self._name = persona_name
        self._role = persona_role
        self._style = persona_style
        self._boundaries = persona_boundaries
        self._remind_after = max(2, remind_after)

    def _reminder(self) -> str:
        return render_prompt(
            "persona_remind.j2",
            name=self._name,
            role=self._role,
            style=self._style,
            boundaries=self._boundaries,
        )

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        if len(request.messages) >= self._remind_after:
            request = append_system_fragment(request, self._reminder())
        return await handler(request)


__all__ = ["DEFAULT_REMIND_AFTER", "PersonaMiddleware"]
