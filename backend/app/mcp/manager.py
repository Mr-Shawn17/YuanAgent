"""MCP 客户端管理器：按 configs/mcp.yaml 连接全部启用的 Server（计划 §P2'）。

设计要点：
- 单服务器失败隔离：某个 Server 连不上只记日志与失败原因，不影响其它 Server 和 Agent 启动；
- 工具经 policy 统一加 `<server>__` 前缀并继承档位，直接并入内置工具注册表；
- 作为 async context manager 使用：进入时连接并发现工具，退出时关闭连接（含 stdio 子进程）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self, cast

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from langchain_core._api import suppress_langchain_beta_warning
from langchain_core.tools import BaseTool

from app.mcp.config import (
    TRANSPORT_STREAMABLE_HTTP,
    McpConfig,
    McpServerConfig,
    load_mcp_config,
)
from app.mcp.policy import apply_mcp_policy
from app.tools.permissions import PermissionTier

with suppress_langchain_beta_warning():
    from langchain.mcp import MCPAdapter

logger = logging.getLogger(__name__)


def build_transport(server: McpServerConfig) -> StdioTransport | StreamableHttpTransport:
    """按配置构造 fastmcp 传输层；stdio 的命令/参数/工作目录已在 config 里解析为绝对路径。"""
    if server.transport == TRANSPORT_STREAMABLE_HTTP:
        return StreamableHttpTransport(url=server.url, headers=server.headers or None)
    return StdioTransport(
        command=server.resolved_command,
        args=server.resolved_args,
        env=server.env or None,
        cwd=server.resolved_cwd,
    )


class McpManager:
    """连接一组 MCP Server，汇总可供 Agent 使用的 LangChain 工具与权限档位。

    用法::

        async with McpManager(load_mcp_config()) as manager:
            registry.tools.extend(manager.tools)
            registry.tiers.update(manager.tiers)
    """

    def __init__(self, config: McpConfig | None = None) -> None:
        self.config = config if config is not None else load_mcp_config()
        # MCPAdapter 会把传入的 Client 克隆一份（装上 elicitation 处理器），
        # 工具实际持有的是克隆体，所以退出时关的是 adapter.client 而不是原始对象。
        self._adapters: list[MCPAdapter] = []
        self.tools: list[BaseTool] = []
        self.tiers: dict[str, PermissionTier] = {}
        self.connected: list[str] = []
        self.failed: dict[str, str] = {}

    async def __aenter__(self) -> Self:
        for server in self.config.enabled_servers():
            try:
                await self._connect(server)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                logger.warning("MCP Server %s 连接失败，已跳过：%s", server.name, reason)
                self.failed[server.name] = reason
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """关闭全部连接；stdio 的 keep_alive 子进程也在这里回收。"""
        for adapter in self._adapters:
            client = adapter.client
            if isinstance(client, Client):
                # fastmcp 的 close 缺少类型标注，转成已标注签名以通过 mypy strict
                close = cast("Callable[[], Awaitable[None]]", client.close)
                try:
                    await close()
                except Exception as exc:
                    logger.debug("关闭 MCP 连接时出错（忽略）：%s", exc)
        self._adapters.clear()

    async def _connect(self, server: McpServerConfig) -> None:
        transport = build_transport(server)
        client: Client[StdioTransport | StreamableHttpTransport] = Client(
            transport, timeout=server.timeout_seconds, mode=server.mode
        )
        adapter = MCPAdapter(client)
        # list_tools 自带连接上下文，返回的工具在上下文退出后仍可调用（每次调用内部重连）
        remote_tools = await adapter.list_tools()
        toolset = apply_mcp_policy(server, remote_tools)
        self._adapters.append(adapter)
        self.tools.extend(toolset.tools)
        self.tiers.update(toolset.tiers)
        self.connected.append(server.name)
        if toolset.skipped:
            logger.info(
                "MCP Server %s 有 %d 个工具不在白名单内，已过滤：%s",
                server.name,
                len(toolset.skipped),
                ", ".join(toolset.skipped),
            )
        logger.info(
            "MCP Server %s 已连接，接入 %d 个工具（档位 %s）：%s",
            server.name,
            len(toolset.tools),
            toolset.tier,
            ", ".join(toolset.names),
        )

    def summary_lines(self) -> list[str]:
        """给 CLI header 用的一行摘要。"""
        lines: list[str] = []
        for name in self.connected:
            count = sum(1 for tool_name in self.tiers if tool_name.startswith(f"{name}__"))
            lines.append(f"  {name}: {count} 个工具")
        for name, reason in self.failed.items():
            lines.append(f"  {name}: 连接失败（{reason}）")
        return lines


__all__ = ["McpManager", "build_transport"]
