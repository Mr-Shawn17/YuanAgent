"""intent_note 中间件：意图预判层的每轮路由指令/提示注入。

预判层（app/intent/gate.py）在每轮进入主 Agent 前完成快判与三段判定，把
DIRECT（直走指令）或 ESCALATE（top-k 强提示）写进本轮 state.intent_note；
本中间件在每个模型调用前把该片段追加到 system prompt 末尾。

注入而非硬编排：主 Agent 仍走既有工具/审计/审批链路（D9：直走与回落都保留
tool_audit 全量落库），只是路由判断被指令引导或携带强提示。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest

from app.agents.builder import append_system_fragment


def _state_note(state: Any) -> str:
    if isinstance(state, dict):
        value = state.get("intent_note", "")
    else:
        value = getattr(state, "intent_note", "")
    return value if isinstance(value, str) else ""


class IntentNoteMiddleware(AgentMiddleware):
    """把 state.intent_note 追加到 system prompt（空串不注入，零改动直通）。"""

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        note = _state_note(request.state)
        if note:
            request = append_system_fragment(request, note)
        return await handler(request)


__all__ = ["IntentNoteMiddleware"]
