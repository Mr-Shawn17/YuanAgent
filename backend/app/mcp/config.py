"""configs/mcp.yaml 的 schema 与加载（计划 §7.4 / P2'）。

接入一个第三方 MCP Server 只改 yaml、不改代码：
每个 server 一段配置，声明 transport（stdio / streamable_http）、权限档位与工具白名单。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import PROJECT_ROOT, load_yaml_config
from app.tools.permissions import PERMISSION_TIERS, TIER_WRITE, PermissionTier, normalize_tier

McpTransport = Literal["stdio", "streamable_http"]

TRANSPORT_STDIO: McpTransport = "stdio"
TRANSPORT_STREAMABLE_HTTP: McpTransport = "streamable_http"

MCP_CONFIG_NAME = "mcp"
DEFAULT_TIMEOUT_SECONDS = 30.0

# 工具名会拼成 <server>__<tool> 交给模型，因此 server name 只能用 OpenAI 工具名允许的字符
SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
# 常见写法归一化：fastmcp/客户端生态里 streamable-http、http 都指同一个 transport
_TRANSPORT_ALIASES: dict[str, str] = {
    "stdio": TRANSPORT_STDIO,
    "http": TRANSPORT_STREAMABLE_HTTP,
    "streamable_http": TRANSPORT_STREAMABLE_HTTP,
}
# command 写成这些别名时解析成当前解释器，避免依赖 PATH 里恰好有对的 python
_PYTHON_ALIASES = frozenset({"python", "python3", "python.exe", "python3.exe", "py"})
# 这些后缀的相对路径参数按项目根解析（典型场景：scripts/mock_mcp_server.py）
_SCRIPT_SUFFIXES = frozenset({".py", ".js", ".mjs", ".cjs", ".ts"})


class McpServerConfig(BaseModel):
    """单个 MCP Server 的接入配置。"""

    name: str
    transport: McpTransport = TRANSPORT_STDIO
    command: str = "python"
    args: list[str] = Field(default_factory=list)
    url: str = ""
    enabled: bool = True
    # 协议协商模式：auto=先探测 server/discover（新协议，失败回退）；legacy=强制旧版握手。
    # 旧版服务端（协议 2024-11-05）对新协议的 server/discover 返回 404，须显式设 legacy。
    mode: str = "auto"
    # 留空时按 write 处理：未知来源的工具默认需要用户确认，比默认放行安全
    permission_tier: str = ""
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=600)
    # 白名单，留空表示该 Server 暴露的工具全部接入
    allow_tools: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            msg = "MCP Server 的 name 不能为空"
            raise ValueError(msg)
        if not SERVER_NAME_PATTERN.match(name):
            msg = f"MCP Server 的 name 非法：{value}（只允许字母、数字、下划线、连字符）"
            raise ValueError(msg)
        return name

    @field_validator("transport", mode="before")
    @classmethod
    def _normalize_transport(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        key = value.strip().lower().replace("-", "_")
        transport = _TRANSPORT_ALIASES.get(key, key)
        if transport not in {TRANSPORT_STDIO, TRANSPORT_STREAMABLE_HTTP}:
            msg = f"MCP transport 非法：{value}，可选 stdio / streamable_http"
            raise ValueError(msg)
        return transport

    @field_validator("permission_tier")
    @classmethod
    def _normalize_permission_tier(cls, value: str) -> str:
        tier = value.strip().lower()
        if tier and tier not in PERMISSION_TIERS:
            msg = f"permission_tier 非法：{value}，可选 read/write/danger"
            raise ValueError(msg)
        return tier

    @field_validator("url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        return value.strip()

    @field_validator("mode")
    @classmethod
    def _normalize_mode(cls, value: str) -> str:
        mode = value.strip().lower()
        if mode not in {"auto", "legacy"}:
            msg = f"mode 非法：{value}，可选 auto/legacy"
            raise ValueError(msg)
        return mode

    @model_validator(mode="after")
    def _check_transport_fields(self) -> Self:
        if self.transport == TRANSPORT_STDIO:
            if not self.command.strip():
                msg = f"MCP Server {self.name} 使用 stdio 时必须配置 command"
                raise ValueError(msg)
        elif not self.url:
            msg = f"MCP Server {self.name} 使用 streamable_http 时必须配置 url"
            raise ValueError(msg)
        return self

    @property
    def tier(self) -> PermissionTier:
        """该 Server 下所有工具继承的权限档位。"""
        return normalize_tier(self.permission_tier, TIER_WRITE)

    @property
    def resolved_command(self) -> str:
        """stdio 启动命令：python 系列别名解析为当前解释器的绝对路径。"""
        command = self.command.strip()
        if command.lower() in _PYTHON_ALIASES:
            return sys.executable
        return command

    @property
    def resolved_args(self) -> list[str]:
        """stdio 启动参数：脚本类的相对路径按项目根解析，其余原样保留。"""
        resolved: list[str] = []
        for item in self.args:
            text = item.strip()
            candidate = Path(text)
            if candidate.suffix.lower() in _SCRIPT_SUFFIXES and not candidate.is_absolute():
                text = str((PROJECT_ROOT / candidate).resolve())
            resolved.append(text)
        return resolved

    @property
    def resolved_cwd(self) -> str:
        """stdio 子进程工作目录，留空为项目根。"""
        raw = self.cwd.strip()
        if not raw:
            return str(PROJECT_ROOT)
        path = Path(raw)
        return str(path if path.is_absolute() else (PROJECT_ROOT / path).resolve())


class McpConfig(BaseModel):
    """configs/mcp.yaml 的顶层结构。"""

    servers: list[McpServerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_names(self) -> Self:
        seen: set[str] = set()
        for server in self.servers:
            if server.name in seen:
                msg = f"MCP Server 名称重复：{server.name}"
                raise ValueError(msg)
            seen.add(server.name)
        return self

    def enabled_servers(self) -> list[McpServerConfig]:
        return [server for server in self.servers if server.enabled]

    def get(self, name: str) -> McpServerConfig | None:
        for server in self.servers:
            if server.name == name:
                return server
        return None


def load_mcp_config(name: str = MCP_CONFIG_NAME, *, directory: Path | None = None) -> McpConfig:
    """读取 MCP 配置；文件不存在时返回空配置（没有 MCP Server 也能正常跑）。"""
    return load_yaml_config(name, McpConfig, directory=directory)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MCP_CONFIG_NAME",
    "TRANSPORT_STDIO",
    "TRANSPORT_STREAMABLE_HTTP",
    "McpConfig",
    "McpServerConfig",
    "McpTransport",
    "load_mcp_config",
]
