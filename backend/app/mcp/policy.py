"""MCP 工具的命名空间、白名单与权限档位策略（计划 §6 / P2'）。

第三方 Server 的工具名可能与内置工具冲突（例如 rag server 也叫 search_knowledge_base），
统一加 `<server>__` 前缀做命名空间；yaml 里的 permission_tier 继承给该 Server 的全部工具，
于是 MCP 工具自动复用已有的「三档权限 + 审批 + 审计」链路，不需要为 MCP 单开一套安全逻辑。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool

from app.mcp.config import McpServerConfig
from app.tools.permissions import PermissionTier

# 工具名前缀分隔符：mock__echo、rag__search_knowledge_base
TOOL_NAME_SEPARATOR = "__"


def namespaced_tool_name(server_name: str, tool_name: str) -> str:
    return f"{server_name}{TOOL_NAME_SEPARATOR}{tool_name}"


def is_allowed(tool_name: str, server_name: str, allow_tools: Sequence[str]) -> bool:
    """白名单判定：留空表示全放行；否则原始名或带前缀名命中任一即可。"""
    if not allow_tools:
        return True
    prefixed = namespaced_tool_name(server_name, tool_name)
    return any(item in (tool_name, prefixed) for item in allow_tools)


def rename_tool(tool: BaseTool, new_name: str) -> BaseTool:
    """复制一份工具并改名：不原地修改 MCPAdapter 返回的对象，避免副作用外泄。"""
    return tool.model_copy(update={"name": new_name})


@dataclass(slots=True)
class McpToolSet:
    """一个 Server 经策略处理后的产物：改名后的工具 + 档位映射 + 被过滤掉的工具名。"""

    server_name: str
    tier: PermissionTier
    tools: list[BaseTool] = field(default_factory=list)
    tiers: dict[str, PermissionTier] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [tool.name for tool in self.tools]


def apply_mcp_policy(server: McpServerConfig, tools: Sequence[BaseTool]) -> McpToolSet:
    """对远端工具列表套用白名单、命名空间与档位继承。纯函数，可离线单测。"""
    toolset = McpToolSet(server_name=server.name, tier=server.tier)
    for tool in tools:
        if not is_allowed(tool.name, server.name, server.allow_tools):
            toolset.skipped.append(tool.name)
            continue
        prefixed = namespaced_tool_name(server.name, tool.name)
        toolset.tools.append(rename_tool(tool, prefixed))
        toolset.tiers[prefixed] = toolset.tier
    return toolset


__all__ = [
    "TOOL_NAME_SEPARATOR",
    "McpToolSet",
    "apply_mcp_policy",
    "is_allowed",
    "namespaced_tool_name",
    "rename_tool",
]
