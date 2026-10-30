from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from app.core.config import PROJECT_ROOT, load_yaml_config
from app.tools.base import ToolContext, ToolEntryConfig
from app.tools.builtin import BUILTIN_TOOL_SPECS
from app.tools.permissions import PermissionTier, normalize_tier


class ToolsConfig(BaseModel):
    """configs/tools.yaml 的顶层结构。"""

    sandbox_root: str = "data/workspace"  # 相对路径按 PROJECT_ROOT 解析
    default_allow: bool = False  # False 时未在 yaml 中列出的工具一律禁用
    tools: dict[str, ToolEntryConfig] = Field(default_factory=dict)


def load_tools_config(name: str = "tools") -> ToolsConfig:
    return load_yaml_config(name, ToolsConfig)


@dataclass(slots=True)
class ToolRegistry:
    """装配结果：可交给 create_agent 的工具列表 + 档位映射 + 禁用原因。"""

    tools: list[BaseTool] = field(default_factory=list)
    tiers: dict[str, PermissionTier] = field(default_factory=dict)
    disabled: dict[str, str] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return [item.name for item in self.tools]

    def tier_of(self, tool_name: str, default: PermissionTier = "write") -> PermissionTier:
        """查工具档位；未登记的（如运行期动态加载的 MCP 工具）保守按 write 处理。"""
        return self.tiers.get(tool_name, default)


def _resolve_sandbox_root(config: ToolsConfig, context: ToolContext) -> Path:
    raw = config.sandbox_root.strip()
    if not raw:
        return context.settings.workspace_dir.resolve()
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def build_tools(context: ToolContext, config: ToolsConfig | None = None) -> ToolRegistry:
    """按 tools.yaml 装配内置工具：判档、检查密钥依赖、记录禁用原因。"""
    resolved = config if config is not None else load_tools_config()
    registry = ToolRegistry()
    sandbox_root = _resolve_sandbox_root(resolved, context)
    if context.sandbox_root is None:
        context = replace(context, sandbox_root=sandbox_root)

    for name, spec in BUILTIN_TOOL_SPECS.items():
        entry = resolved.tools.get(name)
        if entry is None:
            if not resolved.default_allow:
                registry.disabled[name] = "未在 tools.yaml 中启用（default_allow=false）"
                continue
            entry = ToolEntryConfig()
        if not entry.enabled:
            registry.disabled[name] = "已在 tools.yaml 中禁用"
            continue

        requires_key = entry.requires_key or spec.default_requires_key
        if requires_key and not context.settings.has_secret(requires_key):
            registry.disabled[name] = f"缺少 {requires_key}，自动禁用"
            continue

        built = spec.factory(context, entry)
        if built is None:
            registry.disabled[name] = "工厂未产出工具（依赖缺失）"
            continue
        registry.tools.append(built)
        registry.tiers[name] = normalize_tier(entry.tier, spec.default_tier)

    for name in resolved.tools:
        if name not in BUILTIN_TOOL_SPECS:
            registry.disabled[name] = "未知的工具名（没有对应的内置工厂）"
    return registry


__all__ = ["ToolRegistry", "ToolsConfig", "build_tools", "load_tools_config"]
