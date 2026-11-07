"""ShellBackend：策略层直跑（零依赖默认档）。

cmd.exe /c（或 powershell -Command）执行，环境白名单传递，超时 psutil 杀整棵进程树。
隔离强度 = 策略层（白名单/黑名单/审批/审计），非系统级隔离——需要更强隔离时
配置 command.backend 走 windows_sandbox 档。
"""

from __future__ import annotations

import asyncio
import contextlib
import locale
from pathlib import Path

from runner.command import CommandPolicy, CommandResult, build_sandbox_env, kill_process_tree


def _decode(data: bytes) -> str:
    """按 utf-8 → 本地编码顺序解码（cmd 输出常为 GBK，git 输出为 UTF-8）。"""
    for encoding in ("utf-8", locale.getpreferredencoding(False) or "utf-8"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class ShellBackend:
    """默认命令后端：进程直跑 + 策略护栏。"""

    name = "shell"

    def __init__(self, policy: CommandPolicy) -> None:
        self._policy = policy
        self._shell = policy.shell.strip().lower()

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: int,  # noqa: ASYNC109 — 整段执行的超时上限（含进程树清理）
        extra_env: dict[str, str] | None = None,
        workspace: Path | None = None,
    ) -> CommandResult:
        env = build_sandbox_env(self._policy, extra_env)
        if self._shell == "powershell":
            argv = (
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", command,
            )
        else:
            argv = ("cmd.exe", "/d", "/c", command)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            return CommandResult(
                exit_code=None, timed_out=False, stdout="",
                stderr=f"无法启动 {argv[0]}：{exc}", backend=self.name,
            )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            await asyncio.to_thread(kill_process_tree, proc.pid)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            out, err = await proc.communicate()
            return CommandResult(
                exit_code=proc.returncode, timed_out=True,
                stdout=_decode(out or b""), stderr=_decode(err or b""),
                backend=self.name,
            )
        return CommandResult(
            exit_code=proc.returncode, timed_out=False,
            stdout=_decode(out or b""), stderr=_decode(err or b""),
            backend=self.name,
        )


__all__ = ["ShellBackend"]
