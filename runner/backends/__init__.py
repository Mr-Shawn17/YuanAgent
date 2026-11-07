"""命令执行后端装配：按配置与机器条件选择 shell / windows_sandbox。

auto：Windows Sandbox 功能已启用且工具链就绪 → VM 档；否则策略层直跑。
显式 windows_sandbox：不可用时直接报错（不静默降级，避免用户误以为在 VM 中执行）。
"""

from __future__ import annotations

import logging

from runner.command import CommandBackend, CommandPolicy

logger = logging.getLogger("runner.backends")


def create_backend(policy: CommandPolicy) -> CommandBackend:
    preferred = policy.backend.strip().lower()
    if preferred in ("auto", "windows_sandbox"):
        from runner.backends.windows_sandbox import WindowsSandboxBackend, is_sandbox_available

        available = is_sandbox_available()
        if preferred == "windows_sandbox" and not available:
            msg = (
                "Windows Sandbox 档已显式配置但功能未启用："
                "请在「启用或关闭 Windows 功能」中开启「Windows 沙盒」"
                "（需 Windows Pro + 重启），或把 command.backend 改为 auto/shell"
            )
            raise RuntimeError(msg)
        if available:
            logger.info("command_backend=windows_sandbox")
            return WindowsSandboxBackend(policy)
    from runner.backends.shell import ShellBackend

    logger.info("command_backend=shell")
    return ShellBackend(policy)


__all__ = ["create_backend"]
