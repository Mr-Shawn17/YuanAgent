"""skill 中间件：每次模型调用前把「可用技能清单」注入 system prompt。

渐进式披露的前半段：清单只含名称 + 一行 description（几十 token），
指南全文由模型按需调用 load_skill 工具获取（后半段）。
清单在中间件构造时渲染一次并复用（启动到重启之间技能集合不变）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from langchain.agents.middleware import AgentMiddleware, ModelRequest

from app.agents.builder import append_system_fragment
from app.skills.schema import SkillRecord

SKILLS_PROMPTS_DIR = Path(__file__).parents[1] / "skills" / "prompts"


@lru_cache(maxsize=1)
def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(SKILLS_PROMPTS_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )


def render_skill_list(records: list[SkillRecord]) -> str:
    """渲染技能清单片段；无技能时返回空串（中间件直接透传）。"""
    if not records:
        return ""
    template = _jinja_env().get_template("skills.j2")
    return template.render(records=records).strip()


class SkillMiddleware(AgentMiddleware):
    """每轮 model call 把技能清单追加进 system prompt（空清单时不做任何事）。"""

    def __init__(self, fragment: str) -> None:
        super().__init__()
        self._fragment = fragment.strip()

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        if self._fragment:
            request = append_system_fragment(request, self._fragment)
        return await handler(request)


def make_skill_middleware(records: list[SkillRecord]) -> SkillMiddleware:
    """按已加载的技能构造中间件；空清单返回空片段中间件（透传）。"""
    return SkillMiddleware(render_skill_list(records))


__all__ = ["SkillMiddleware", "make_skill_middleware", "render_skill_list"]
