from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, cast

PermissionTier = Literal["read", "write", "danger"]

TIER_READ: PermissionTier = "read"
TIER_WRITE: PermissionTier = "write"
TIER_DANGER: PermissionTier = "danger"

PERMISSION_TIERS: frozenset[str] = frozenset({TIER_READ, TIER_WRITE, TIER_DANGER})
# write / danger 两档在执行前必须获得用户确认（计划 §6：调用前判档 → 触发确认 → 全量审计）
CONFIRM_REQUIRED_TIERS: frozenset[str] = frozenset({TIER_WRITE, TIER_DANGER})


def normalize_tier(value: str, default: PermissionTier = TIER_READ) -> PermissionTier:
    tier = value.strip().lower()
    if not tier:
        return default
    if tier not in PERMISSION_TIERS:
        msg = f"未知的工具权限档位：{value}，可选 read/write/danger"
        raise ValueError(msg)
    return cast("PermissionTier", tier)


def requires_confirmation(tier: str) -> bool:
    return tier.strip().lower() in CONFIRM_REQUIRED_TIERS


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """一次待审批的工具调用描述，由权限中间件构造后交给审批回调。"""

    tool_name: str
    tier: PermissionTier
    tool_args: dict[str, Any] = field(default_factory=dict)

    def summary(self, max_length: int = 200) -> str:
        parts: list[str] = []
        for key, value in self.tool_args.items():
            text = repr(value)
            if len(text) > 60:
                text = text[:57] + "..."
            parts.append(f"{key}={text}")
        rendered = f"{self.tool_name}({', '.join(parts)})"
        if len(rendered) > max_length:
            rendered = rendered[: max_length - 1] + "…"
        return rendered


# 审批回调协议：CLI 场景传同步询问（asyncio.to_thread(console.input)），
# P5' Web 场景换成基于 interrupt 的异步等待，接口保持不变。
ApprovalCallback = Callable[[ToolCallRequest], Awaitable[bool]]


async def approve_everything(request: ToolCallRequest) -> bool:
    """全部放行，仅用于测试或用户显式开启的自动模式。"""
    return True


async def deny_everything(request: ToolCallRequest) -> bool:
    """无人值守且未配置审批人时的兜底：write/danger 一律拒绝，避免静默执行敏感操作。"""
    return False


__all__ = [
    "CONFIRM_REQUIRED_TIERS",
    "PERMISSION_TIERS",
    "TIER_DANGER",
    "TIER_READ",
    "TIER_WRITE",
    "ApprovalCallback",
    "PermissionTier",
    "ToolCallRequest",
    "approve_everything",
    "deny_everything",
    "normalize_tier",
    "requires_confirmation",
]
