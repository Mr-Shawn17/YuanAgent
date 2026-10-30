from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.tools.base import ToolContext, ToolEntryConfig, tool_error_boundary

MAX_RESULTS_LIMIT = 10
SNIPPET_LIMIT = 300


def make_web_search_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool | None:
    api_key = context.settings.tavily_api_key.get_secret_value().strip()
    if not api_key:
        # 密钥缺失时返回 None，由 registry 记录禁用原因
        return None

    @tool("web_search")
    @tool_error_boundary
    async def web_search(query: str, max_results: int = 5) -> str:
        """用 Tavily 搜索引擎检索互联网，获取实时资讯、天气、新闻等最新信息。

        Args:
            query: 搜索关键词。
            max_results: 最多返回的结果条数，默认 5，上限 10。
        """
        from tavily import TavilyClient

        limit = max(1, min(int(max_results), MAX_RESULTS_LIMIT))
        client = TavilyClient(api_key=api_key)
        payload: Any = await asyncio.to_thread(client.search, query=query, max_results=limit)
        results = payload.get("results") or []
        if not results:
            return f"未搜索到与「{query}」相关的结果。"

        lines = [f"「{query}」的搜索结果（共 {len(results)} 条）："]
        for index, item in enumerate(results, start=1):
            title = str(item.get("title") or "无标题").strip()
            url = str(item.get("url") or "").strip()
            content = str(item.get("content") or "").strip()
            if len(content) > SNIPPET_LIMIT:
                content = content[:SNIPPET_LIMIT] + "…"
            lines.append(f"{index}. {title}\n   链接：{url}\n   摘要：{content}")
        return "\n".join(lines)

    return web_search


__all__ = ["make_web_search_tool"]
