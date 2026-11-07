"""run_command 工具：在工作目录执行 CLI 命令（danger 档，审批 + 审计由中间件强制）。

两种执行位置（与文件三件套同模式）：
- Web 模式（mode: local 且注入 router）：经 LocalTaskHub 转发到用户电脑上的
  YuanRunner，策略层与沙箱档位在 runner 侧执行（runner/command.py）
- CLI 模式（无 router）：本进程直跑策略层——本文件下半部分与 runner/command.py
  **同规则复制**（两边改动必须双向同步，注释互指）

策略层（防 LLM 失控/提示注入）：白名单 + 危险模式黑名单 + cwd 沙箱
+ 环境变量白名单（绝不透传密钥） + 超时杀整棵进程树 + 输出封顶。
"""

from __future__ import annotations

import asyncio
import contextlib
import locale
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import psutil
from langchain_core.tools import BaseTool, tool

from app.tools.base import (
    ToolContext,
    ToolEntryConfig,
    resolve_in_sandbox,
    tool_error_boundary,
)

# ---------------------------------------------------------------------------
# 与 runner/command.py 同规则的策略常量（双向同步）
# ---------------------------------------------------------------------------

ALLOWED_COMMANDS = (
    "git", "python", "python3", "py", "pip", "pip3", "uv", "conda",
    "pytest", "ruff", "mypy", "black", "isort",
    "node", "npm", "npx", "pnpm", "yarn", "tsc", "deno", "bun",
    "cargo", "rustc", "go", "make", "cmake",
    "java", "javac", "mvn", "gradle", "dotnet",
    "gcc", "g++", "clang", "clang++",
    "ls", "dir", "cat", "type", "echo", "cd", "pwd", "mkdir", "md", "touch",
    "cp", "copy", "move", "mv", "rm", "del", "find", "findstr", "grep", "rg",
    "sed", "awk", "head", "tail", "wc", "tree", "where", "which", "sort",
    "uniq", "diff", "tar", "zip", "unzip", "7z", "curl", "wget",
    "ping", "ipconfig", "netstat", "tasklist", "whoami", "hostname", "date",
    "env", "set", "less", "more", "code", "vim",
)

BLOCKED_PATTERNS = (
    r"rm\s+(-\w+\s+)*(/|~|%USERPROFILE%|\$HOME)(\s|$)",
    r"\bdel\s+[^&|]*\s/s\b",
    r"\b(rd|rmdir)\s+[^&|]*\s/s\b",
    r"\bformat\s+[a-z]:",
    r"\bdiskpart\b",
    r"\bcipher\b[^&|]*\s/w\b",
    r"\breg(edy)?\w*\s+(add|delete|import|restore)\b",
    r"\bregedit\b(\s+/s)?\b",
    r"powershell(\.exe)?\s+[^|]*\s-(e|ec|enc|encodedcommand)\b",
    r"powershell(\.exe)?\s+[^|]*\b(iex|invoke-expression)\b",
    r"\bshutdown\b",
    r"\btaskkill\b[^&|]*\s/f\b",
    r"\bnet\s+(user|localgroup|stop|start)\b",
    r"\bschtasks\b",
    r"\bmshta\b",
    r"\brundll32\b",
    r"\b(c|w)script\b",
    r"\bvssadmin\b",
    r"\b(set|add)-mppreference\b",
    r"(curl|wget|iwr|invoke-webrequest)[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b",
)

ENV_WHITELIST = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "WINDIR",
    "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONUTF8",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMFILES", "PROGRAMDATA",
)

_SENSITIVE_ENV_RE = re.compile(r"(api_?key|secret|token|password|passwd|credential)", re.IGNORECASE)
_COMMAND_SPLIT_RE = re.compile(r"&&|\|\||[|;\r\n]")
_ALLOWED_SET = frozenset(ALLOWED_COMMANDS)
_BLOCKED_COMPILED = tuple(re.compile(p, re.IGNORECASE) for p in BLOCKED_PATTERNS)


class CommandBlockedError(Exception):
    """命令被策略层拦截（与 runner/command.CommandBlockedError 同语义）。"""


def check_policy(command: str) -> None:
    """白名单 + 黑名单检查；多命令段（&&、管道）逐段查首 token。"""
    text = (command or "").strip()
    if not text:
        raise CommandBlockedError("命令为空")
    if len(text) > 8000:
        raise CommandBlockedError("命令过长（超过 8000 字符）")
    for regex in _BLOCKED_COMPILED:
        if regex.search(text):
            raise CommandBlockedError(f"命中危险命令模式：{regex.pattern}")
    for segment in _COMMAND_SPLIT_RE.split(text):
        parts = segment.strip().split()
        if not parts:
            continue
        token = parts[0].strip("\"'").lower()
        token = Path(token).name
        if token.endswith(".exe"):
            token = token[:-4]
        if token and token not in _ALLOWED_SET:
            raise CommandBlockedError(f"命令不在白名单内：{token}")


def build_sandbox_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """构造子进程环境：白名单 + 显式注入项，敏感名剔除。"""
    extras = extra_env or {}
    env: dict[str, str] = {}
    for name in ENV_WHITELIST:
        value = extras.get(name) or os.environ.get(name)
        if value:
            env[name] = value
    for name, value in extras.items():
        if value:
            env[name] = value
    return {name: value for name, value in env.items() if not _SENSITIVE_ENV_RE.search(name)}


def kill_process_tree(pid: int) -> None:
    """杀 pid 及其全部子孙（与 runner/command.kill_process_tree 同规则）。"""
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    procs = [*parent.children(recursive=True), parent]
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(procs, timeout=5)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", locale.getpreferredencoding(False) or "utf-8"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def execute_subprocess(
    argv: list[str], *, cwd: Path, timeout: int, env: dict[str, str]
) -> tuple[int | None, bool, str, str]:
    """同步执行（阻塞，调用方放线程池）：返回 (exit_code, timed_out, stdout, stderr)。"""
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        return None, False, "", f"无法启动 {argv[0]}：{exc}"
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        with contextlib.suppress(Exception):
            proc.kill()
        out, err = proc.communicate()
        return proc.returncode, True, _decode(out or b""), _decode(err or b"")
    return proc.returncode, False, _decode(out or b""), _decode(err or b"")


def _format(
    exit_code: int | None,
    timed_out: bool,
    merged: str,
    *,
    max_chars: int,
    redact: tuple[str, ...] = (),
    timeout_seconds: int = 0,
) -> str:
    for secret in redact:
        if secret:
            merged = merged.replace(secret, "***")
    if timed_out:
        merged = merged[:max_chars]
        brief = f"[执行超时] 命令超过 {timeout_seconds} 秒被终止（进程树已结束）"
        if merged.strip():
            return f"{brief}。部分输出：\n{merged}"
        return brief
    if len(merged) > max_chars:
        merged = merged[:max_chars] + f"\n…（输出超长，已截断至 {max_chars} 字符）"
    status = "exit 0" if exit_code == 0 else f"exit {exit_code}"
    if not merged.strip():
        return f"（命令无输出，{status}）"
    return f"[{status}]\n{merged}"


def run_command_core(
    command: str,
    *,
    cwd: Path,
    timeout: int,
    max_chars: int = 20_000,
    shell: str = "cmd",
    redact: tuple[str, ...] = (),
    env_extra: dict[str, str] | None = None,
) -> str:
    """CLI 模式的本地执行：策略检查 → cmd/powershell → 封顶格式化。

    env_extra 仅供内部结构化 git 工具注入凭证（变量名避开敏感词），
    LLM 直发的命令永远不传。
    """
    check_policy(command)
    if shell == "powershell":
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", command]
    else:
        argv = ["cmd.exe", "/d", "/c", command]
    exit_code, timed_out, out, err = execute_subprocess(
        argv, cwd=cwd, timeout=timeout, env=build_sandbox_env(env_extra)
    )
    merged = out + (f"\n[stderr]\n{err}" if err.strip() else "")
    return _format(
        exit_code, timed_out, merged, max_chars=max_chars, redact=redact,
        timeout_seconds=timeout,
    )


def run_argv_core(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
    max_chars: int = 20_000,
    env_extra: dict[str, str] | None = None,
    label: str = "脚本",
) -> str:
    """可信 argv 执行（skills 脚本等内部调用：不查命令白名单，环境仍白名单）。"""
    exit_code, timed_out, out, err = execute_subprocess(
        argv, cwd=cwd, timeout=timeout, env=build_sandbox_env(env_extra)
    )
    merged = out + (f"\n[stderr]\n{err}" if err.strip() else "")
    if timed_out:
        return (
            f"[脚本执行超时] {label} 超过 {timeout} 秒被终止（进程树已结束）；"
            "可调大超时或优化脚本"
        )
    return _format(exit_code, False, merged, max_chars=max_chars)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------

DEFAULT_COMMAND_TIMEOUT = 120
MAX_COMMAND_TIMEOUT = 600


def _wants_local(entry: ToolEntryConfig, context: ToolContext) -> bool:
    return entry.mode.strip().lower() == "local" and context.local_router is not None


def dispatch_local(
    context: ToolContext,
    *,
    tool_name: str,
    args: dict[str, Any],
    timeout: float | None = None,
) -> str:
    """local 模式转发（与 filesystem._dispatch_local 同款，多 timeout 参数）。"""
    router: Any = context.local_router
    assert router is not None
    result: str = asyncio.run(
        router.dispatch(
            user_id=context.user_id,
            tool=tool_name,
            args=args,
            workspace=context.workspace,
            timeout=timeout,
        )
    )
    return result


def make_run_command_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    root = context.sandbox()
    use_local = _wants_local(entry, context)

    @tool("run_command")
    @tool_error_boundary
    def run_command(
        command: str, timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT, cwd: str = "."
    ) -> str:
        """在用户工作目录执行一条 CLI 命令（白名单校验 + 超时 + 输出封顶）。
        属于危险操作，执行前需要用户确认。

        Args:
            command: 要执行的命令行文本（支持 && 与管道，每个子命令都须在白名单内：
                git/python/node/npm 等，见文档）。在独立的受限子进程环境中执行，
                不继承 API 密钥等敏感环境变量。
            timeout_seconds: 超时秒数，默认 120，最大 600，超时后整棵进程树被终止。
            cwd: 工作目录内的相对路径，默认为工作目录根。
        """
        timeout = max(1, min(int(timeout_seconds), MAX_COMMAND_TIMEOUT))
        if use_local:
            return dispatch_local(
                context,
                tool_name="run_command",
                args={"command": command, "timeout_seconds": timeout, "cwd": cwd},
                timeout=timeout + 45.0,
            )
        target = resolve_in_sandbox(root, cwd or ".")
        return run_command_core(command, cwd=target, timeout=timeout)

    return run_command


__all__ = [
    "CommandBlockedError",
    "build_sandbox_env",
    "check_policy",
    "kill_process_tree",
    "make_run_command_tool",
    "run_argv_core",
    "run_command_core",
]
