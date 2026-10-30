"""MCP 客户端集成包（计划 §P2'）：配置 schema、工具策略、连接管理器。"""

from app.mcp.config import (
    MCP_CONFIG_NAME,
    McpConfig,
    McpServerConfig,
    load_mcp_config,
)
from app.mcp.manager import McpManager
from app.mcp.policy import McpToolSet, apply_mcp_policy, namespaced_tool_name

__all__ = [
    "MCP_CONFIG_NAME",
    "McpConfig",
    "McpManager",
    "McpServerConfig",
    "McpToolSet",
    "apply_mcp_policy",
    "load_mcp_config",
    "namespaced_tool_name",
]
