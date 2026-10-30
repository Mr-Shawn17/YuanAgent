"""本地 mock MCP Server（计划 §P2'），用于验证 MCP 客户端链路。

以 stdio 传输启动：config/mcp.yaml 里 mock 段的 command=python、
args=[scripts/mock_mcp_server.py] 会把它作为子进程拉起。
提供 3 个只读工具：echo / add / now。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastmcp import FastMCP

mcp = FastMCP("mock-mcp")


@mcp.tool
def echo(text: str) -> str:
    """原样回显输入文本，返回 "echo: <text>"。"""
    return f"echo: {text}"


@mcp.tool
def add(a: int, b: int) -> int:
    """计算两个整数之和。"""
    return a + b


@mcp.tool
def now() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    mcp.run(transport="stdio")
