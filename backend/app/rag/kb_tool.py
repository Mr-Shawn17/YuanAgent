from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, tool

from app.rag.schema import KnowledgeChunk
from app.rag.stub_data import StubKnowledgeBase

if TYPE_CHECKING:
    from app.tools.base import ToolContext, ToolEntryConfig

MODE_STUB = "stub"
MODE_MCP = "mcp"
DEFAULT_TOP_K = 5


def _dump_payload(chunks: list[KnowledgeChunk]) -> str:
    """工具返回值统一序列化成 JSON 文本：模型可读，前端/测试可解析。"""
    payload: dict[str, object] = {"count": len(chunks), "chunks": chunks}
    if not chunks:
        payload["hint"] = "知识库中没有检索到相关内容，请如实告知用户，不要编造。"
    return json.dumps(payload, ensure_ascii=False)


def make_kb_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool | None:
    """search_knowledge_base 工厂。

    mode=stub 时用本地语料；mode=mcp 时返回 None（P3' 由 MCP 管理器提供同名工具）。
    """
    mode = (entry.mode or MODE_STUB).strip().lower()
    if mode != MODE_STUB:
        return None
    # 延迟导入断环：app.tools 包初始化会加载 builtin（其中注册了本工厂），
    # 若在模块顶层导入 app.tools.base 会形成 rag ↔ tools 的循环依赖
    from app.tools.base import tool_error_boundary

    kb = StubKnowledgeBase()

    @tool("search_knowledge_base", parse_docstring=True)
    @tool_error_boundary
    async def search_knowledge_base(query: str, top_k: int = DEFAULT_TOP_K) -> str:
        """在个人知识库中检索与 query 相关的资料片段。

        用于回答需要事实依据的问题；返回 JSON 文本，内含带来源（doc_id/title/section/page/uri）
        与相关性分数的片段列表。回答时请引用片段内容并标注来源，检索为空时如实说明。

        Args:
            query: 检索关键词或问题。
            top_k: 最多返回的片段数量，默认 5。
        """
        text = query.strip()
        if not text:
            return _dump_payload([])
        chunks = kb.search(text, top_k=top_k)
        # 引用事件（CitationsEvent）统一由 ToolPermissionMiddleware 在工具成功后发布，
        # 同时覆盖 stub 与 MCP 两种形态，这里不再重复发布
        return _dump_payload(chunks)

    return search_knowledge_base


__all__ = ["DEFAULT_TOP_K", "MODE_MCP", "MODE_STUB", "make_kb_tool"]
