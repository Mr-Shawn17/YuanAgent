"""命令执行核心：策略层 + CommandBackend 抽象 + 命令类工具统一分发。

命令类工具（run_command / git_clone / git_pull / git_push / delegate_coding）
由本模块的 CommandRunner 统一处理，与文件三件套（executor.py）并列。

威胁模型：防 LLM 失控 / 提示注入破坏本机（个人单机，非多租户）。
多层防护（策略层基线；windows_sandbox 档叠加 VM 级隔离，见 backends/）：
1. 命令白名单 + 危险模式黑名单（check_policy，拦截返回 [command_blocked]）
2. cwd 钉死在 workspace 内（复用 executor.resolve_in_sandbox 包含检查）
3. 环境变量白名单 + 敏感名过滤（绝不透传 API key / token 类变量）
4. 超时杀整棵进程树（psutil；Windows Job Object 硬化留待后续）
5. 输出封顶 + git 凭证剥离（gitops 场景 PAT 不出现在任何回传文本）
danger 档审批由服务端 ToolPermissionMiddleware 在工具层强制，本模块不做二次审批。
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import psutil

from runner.executor import SandboxEscapeError, resolve_in_sandbox

# ---------------------------------------------------------------------------
# 默认策略：白名单 / 黑名单 / 环境白名单（均可在 runner.yaml 的 command 段覆盖扩展）
# ---------------------------------------------------------------------------

DEFAULT_ALLOWED_COMMANDS = (
    # 版本管理与包管理
    "git", "python", "python3", "py", "pip", "pip3", "uv", "conda",
    "pytest", "ruff", "mypy", "black", "isort",
    # JS/TS 与其他语言工具链
    "node", "npm", "npx", "pnpm", "yarn", "tsc", "deno", "bun",
    "cargo", "rustc", "go", "make", "cmake",
    "java", "javac", "mvn", "gradle", "dotnet",
    "gcc", "g++", "clang", "clang++",
    # 常用文件/文本命令（危险用法由黑名单与 danger 审批兜底）
    "ls", "dir", "cat", "type", "echo", "cd", "pwd", "mkdir", "md", "touch",
    "cp", "copy", "move", "mv", "rm", "del", "find", "findstr", "grep", "rg",
    "sed", "awk", "head", "tail", "wc", "tree", "where", "which", "sort",
    "uniq", "diff", "tar", "zip", "unzip", "7z", "curl", "wget",
    # 只读系统信息类
    "ping", "ipconfig", "netstat", "tasklist", "whoami", "hostname", "date",
    "env", "set", "less", "more", "code", "vim",
)

# 危险模式黑名单：正则匹配整条命令（大小写不敏感）。命中即拒绝。
DEFAULT_BLOCKED_PATTERNS = (
    r"rm\s+(-\w+\s+)*(/|~|%USERPROFILE%|\$HOME)(\s|$)",  # rm 递归删根/家目录
    r"\bdel\s+[^&|]*\s/s\b",  # del 递归删除
    r"\b(rd|rmdir)\s+[^&|]*\s/s\b",  # rd/rmdir 递归删除
    r"\bformat\s+[a-z]:",  # 格式化盘
    r"\bdiskpart\b",
    r"\bcipher\b[^&|]*\s/w\b",  # 擦除磁盘空闲空间
    r"\breg(edy)?\w*\s+(add|delete|import|restore)\b",  # 注册表写入
    r"\bregedit\b(\s+/s)?\b",
    r"powershell(\.exe)?\s+[^|]*\s-(e|ec|enc|encodedcommand)\b",  # PS base64 隐藏执行
    r"powershell(\.exe)?\s+[^|]*\b(iex|invoke-expression)\b",
    r"\bshutdown\b",
    r"\btaskkill\b[^&|]*\s/f\b",
    r"\bnet\s+(user|localgroup|stop|start)\b",
    r"\bschtasks\b",
    r"\bmshta\b",
    r"\brundll32\b",
    r"\b(c|w)script\b",
    r"\bvssadmin\b",
    r"\b(set|add)-mppreference\b",  # 关闭 Defender
    r"(curl|wget|iwr|invoke-webrequest)[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b",  # 下载管道执行
)

# 子进程环境白名单：git 等工具需要 USERPROFILE 找全局配置，故纳入（仅路径，无密钥）
BASE_ENV_WHITELIST = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "WINDIR",
    "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONUTF8",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMFILES", "PROGRAMDATA",
)

# 敏感环境变量名兜底过滤：即使被误加进白名单也剔除（api key / token / 密码类）
_SENSITIVE_ENV_RE = re.compile(r"(api_?key|secret|token|password|passwd|credential)", re.IGNORECASE)

_COMMAND_SPLIT_RE = re.compile(r"&&|\|\||[|;\r\n]")


class CommandBlockedError(Exception):
    """命令被策略层拦截（白名单外 / 命中危险模式）。"""


@dataclass(slots=True)
class CommandResult:
    """一次命令执行的结果。"""

    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    backend: str = "shell"


@dataclass(slots=True)
class CommandPolicy:
    """runner.yaml 的 command 段：后端选择 + 白名单/黑名单 + 各类上限。"""

    backend: str = "auto"  # auto | shell | windows_sandbox
    shell: str = "cmd"  # cmd | powershell（ShellBackend 用）
    allowed_commands: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_COMMANDS))
    blocked_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_PATTERNS))
    env_whitelist: list[str] = field(default_factory=list)
    timeout_seconds: int = 120
    max_timeout_seconds: int = 600
    max_output_chars: int = 20_000
    sandbox_tools_dir: str = ""  # VM 档便携工具链目录（空=默认 %LOCALAPPDATA%）
    git_user_name: str = "YuanAgent"
    git_user_email: str = "yuanagent@local"
    codex_enabled: bool = True
    codex_path: str = "codex"
    codex_timeout_seconds: int = 600

    @property
    def allowed_set(self) -> frozenset[str]:
        return frozenset(name.strip().lower() for name in self.allowed_commands if name.strip())

    @property
    def blocked_regexes(self) -> tuple[re.Pattern[str], ...]:
        return tuple(_compile_pattern(p) for p in self.blocked_patterns if p.strip())


@dataclass(frozen=True, slots=True)
class CompiledPattern:
    pattern: re.Pattern[str]


_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _compile_pattern(raw: str) -> re.Pattern[str]:
    compiled = _PATTERN_CACHE.get(raw)
    if compiled is None:
        compiled = re.compile(raw, re.IGNORECASE)
        _PATTERN_CACHE[raw] = compiled
    return compiled


class CommandBackend(Protocol):
    """命令执行后端协议：shell（策略层直跑）/ windows_sandbox（VM 档）。"""

    name: str

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: int,  # noqa: ASYNC109 — 协议约定整段执行的超时上限（非单次 await）
        extra_env: dict[str, str] | None = None,
        workspace: Path | None = None,  # VM 档用于会话映射；shell 档忽略
    ) -> CommandResult: ...


def check_policy(command: str, policy: CommandPolicy) -> None:
    """策略检查：白名单 + 黑名单。违规抛 CommandBlockedError（调用方转文本）。

    多命令场景（a && b、管道 a | b）对每个子命令段的首 token 分别做白名单检查，
    防止借道白名单命令夹带执行任意程序。
    """
    text = (command or "").strip()
    if not text:
        raise CommandBlockedError("命令为空")
    if len(text) > 8000:
        raise CommandBlockedError("命令过长（超过 8000 字符）")
    for regex in policy.blocked_regexes:
        if regex.search(text):
            raise CommandBlockedError(f"命中危险命令模式：{regex.pattern}")
    allowed = policy.allowed_set
    for token in _first_tokens(text):
        if token not in allowed:
            raise CommandBlockedError(
                f"命令不在白名单内：{token}（可让用户在 runner.yaml 的 "
                "command.allowed_commands 中扩展）"
            )


def _first_tokens(command: str) -> list[str]:
    """切出各子命令段的首 token（小写、去引号、去 .exe 后缀、取文件名）。"""
    tokens: list[str] = []
    for segment in _COMMAND_SPLIT_RE.split(command):
        parts = segment.strip().split()
        if not parts:
            continue
        token = parts[0].strip("\"'").lower()
        token = Path(token).name  # 带路径的（如 D:\tools\git.exe）取文件名
        if token.endswith(".exe"):
            token = token[:-4]
        if token:
            tokens.append(token)
    return tokens


def build_sandbox_env(
    policy: CommandPolicy, extra_env: dict[str, str] | None = None
) -> dict[str, str]:
    """构造子进程环境：白名单 + 显式注入项，敏感名一律剔除。

    extra_env 仅供内部结构化 git 工具注入凭证（变量名避开敏感词），
    run_command 工具路径永远不传。
    """
    extras = extra_env or {}
    names = set(BASE_ENV_WHITELIST) | {name.strip() for name in policy.env_whitelist}
    env: dict[str, str] = {}
    for name in names:
        value = extras.get(name) or os.environ.get(name)
        if value:
            env[name] = value
    for name, value in extras.items():
        if value:
            env[name] = value
    return {name: value for name, value in env.items() if not _SENSITIVE_ENV_RE.search(name)}


def kill_process_tree(pid: int) -> None:
    """杀掉 pid 及其全部子孙进程（尽力而为；竞态窗口由后续 Job Object 硬化）。"""
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    procs = [*parent.children(recursive=True), parent]
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(procs, timeout=5)


def format_result(
    result: CommandResult,
    *,
    max_chars: int,
    redact: tuple[str, ...] = (),
    timeout_seconds: int = 0,
) -> str:
    """把 CommandResult 格式化为给模型看的文本（封顶 + 凭证剥离）。"""
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    for secret in redact:
        if secret:
            stdout = stdout.replace(secret, "***")
            stderr = stderr.replace(secret, "***")
    merged = stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else "")
    if result.timed_out:
        merged = merged[:max_chars]
        brief = f"[执行超时] 命令超过 {timeout_seconds} 秒被终止（进程树已结束）"
        return f"{brief}。部分输出：\n{merged}" if merged.strip() else brief
    if len(merged) > max_chars:
        merged = merged[:max_chars] + f"\n…（输出超长，已截断至 {max_chars} 字符）"
    status = "exit 0" if result.exit_code == 0 else f"exit {result.exit_code}"
    if not merged.strip():
        return f"（命令无输出，{status}）"
    return f"[{status}]\n{merged}"


class CommandRunner:
    """命令类工具的统一入口：策略检查 → 后端执行 → 格式化。永不抛异常。"""

    def __init__(self, policy: CommandPolicy) -> None:
        self.policy = policy
        self._backend: CommandBackend | None = None

    @property
    def backend(self) -> CommandBackend:
        if self._backend is None:
            from runner.backends import create_backend

            self._backend = create_backend(self.policy)
        return self._backend

    async def execute(self, tool: str, args: dict[str, object], workspace: Path) -> str:
        """分发一条命令类工具请求；任何异常都转为可读错误文本。"""
        try:
            if tool == "run_command":
                return await self._run_command(args, workspace)
            if tool in ("git_clone", "git_pull", "git_push"):
                from runner.gitops import run_git_tool

                return await run_git_tool(tool, args, workspace, self.policy, self.backend)
            if tool == "delegate_coding":
                from runner.coding import run_codex_task

                return await run_codex_task(args, workspace, self.policy)
            return f"[工具执行失败] 未知命令类工具：{tool}"
        except CommandBlockedError as exc:
            return f"[command_blocked] {exc}"
        except SandboxEscapeError as exc:
            return f"[sandbox_escape] {exc}"
        except Exception as exc:  # 错误边界：命令失败不炸连接
            text = f"{type(exc).__name__}: {exc}"[:300]
            return f"[工具执行失败] {text}"

    async def _run_command(self, args: dict[str, object], workspace: Path) -> str:
        from runner.executor import SandboxEscapeError as _Escape

        command = str(args.get("command", "")).strip()
        if not command:
            return "[command_blocked] 命令为空"
        raw_timeout = args.get("timeout_seconds") or self.policy.timeout_seconds
        timeout = max(1, min(int(raw_timeout), self.policy.max_timeout_seconds))
        check_policy(command, self.policy)
        try:
            cwd = resolve_in_sandbox(workspace, str(args.get("cwd", ".") or "."))
        except _Escape as exc:
            return f"[sandbox_escape] {exc}"
        result = await self.backend.run(
            command, cwd=cwd, timeout=timeout, workspace=workspace
        )
        return format_result(
            result,
            max_chars=self.policy.max_output_chars,
            timeout_seconds=timeout,
        )


# 命令类工具名（与 executor.ALLOWED_TOOLS 文件三件套并列，connection 层分流）
COMMAND_TOOLS = frozenset({"run_command", "git_clone", "git_pull", "git_push", "delegate_coding"})


__all__ = [
    "BASE_ENV_WHITELIST",
    "COMMAND_TOOLS",
    "DEFAULT_ALLOWED_COMMANDS",
    "DEFAULT_BLOCKED_PATTERNS",
    "CommandBackend",
    "CommandBlockedError",
    "CommandPolicy",
    "CommandResult",
    "CommandRunner",
    "build_sandbox_env",
    "check_policy",
    "format_result",
    "kill_process_tree",
]
