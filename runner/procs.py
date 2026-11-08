"""结构化子进程执行助手：环境白名单 + 超时杀树 + 输出消毒（开发工具共用）。

服务 git / 依赖安装 / codex 委派三类结构化工具（docs/codex-integration.md）：
- 命令行一律由调用方以 argv 列表拼装，本模块不经手 shell 字符串；
- 子进程环境按白名单重建，绝不透传 API key / token 类变量；
- 超时杀整棵进程树（Windows taskkill /T，POSIX 优先 psutil，缺失则退化为杀主进程）；
- 输出统一解码（utf-8 → 本地编码）、剥离凭证模式、封顶后返回。
"""

from __future__ import annotations

import contextlib
import locale
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT_SECONDS = 600

# 子进程环境白名单：git/npm/python 需要的路径与编码变量（仅路径类，无密钥）。
# git 需要 USERPROFILE 定位全局配置；npm 全局 bin（codex.cmd）在 %APPDATA%\npm。
BASE_ENV_WHITELIST = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "WINDIR",
    "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONUTF8",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMFILES", "PROGRAMDATA", "NODE_PATH",
)

# 敏感变量名兜底过滤：即使被误加进 extras 也剔除。
# git 凭证注入用 YUAN_GIT_USER / YUAN_GIT_PAT 命名，刻意避开这些敏感词。
_SENSITIVE_ENV_RE = re.compile(r"(api_?key|secret|token|password|passwd|credential)", re.IGNORECASE)

# 输出中要剥离的凭证模式（双保险：正常流程凭证本就不该出现在回传文本里）
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"yrt_[A-Za-z0-9_\-]{20,}"),
)
# URL 内嵌凭证：https://user:token@host → https://***@host
_URL_CREDS_RE = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@")


@dataclass(slots=True)
class ProcResult:
    """一次子进程执行的结果。"""

    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str


def build_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """构造子进程环境：白名单 ∪ 显式注入项，敏感名一律剔除。"""
    extras = extra_env or {}
    env: dict[str, str] = {}
    for name in BASE_ENV_WHITELIST:
        value = extras.get(name) or os.environ.get(name)
        if value:
            env[name] = value
    for name, value in extras.items():
        if value:
            env[name] = value
    return {name: value for name, value in env.items() if not _SENSITIVE_ENV_RE.search(name)}


def decode(data: bytes) -> str:
    """按 utf-8 → 本地编码顺序解码（git 输出为 UTF-8，cmd 生态常为 GBK）。"""
    for encoding in ("utf-8", locale.getpreferredencoding(False) or "utf-8"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def sanitize_output(text: str, redact: tuple[str, ...] = ()) -> str:
    """剥离显式凭证与常见 token 模式后返回。"""
    for secret in redact:
        if secret:
            text = text.replace(secret, "***")
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("***", text)
    return _URL_CREDS_RE.sub(r"\1***@", text)


def kill_process_tree(pid: int) -> None:
    """杀掉 pid 及其全部子孙进程（尽力而为，失败不抛）。"""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        return
    try:
        import psutil

        parent = psutil.Process(pid)
        procs = [*parent.children(recursive=True), parent]
        for proc in procs:
            with contextlib.suppress(psutil.Error):
                proc.kill()
        psutil.wait_procs(procs, timeout=5)
    except ImportError:
        with contextlib.suppress(Exception):
            os.kill(pid, 9)


def run_argv(
    argv: tuple[str, ...] | list[str],
    *,
    cwd: Path,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    extra_env: dict[str, str] | None = None,
) -> ProcResult:
    """同步执行一个 argv 命令（无 shell），超时杀进程树后回收输出。"""
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=build_env(extra_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        message = f"无法启动 {argv[0]}：{exc}"
        return ProcResult(exit_code=None, timed_out=False, stdout="", stderr=message)
    try:
        out, err = proc.communicate(timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        with contextlib.suppress(Exception):
            proc.kill()
        out, err = proc.communicate()
        return ProcResult(
            exit_code=proc.returncode, timed_out=True,
            stdout=decode(out or b""), stderr=decode(err or b""),
        )
    return ProcResult(
        exit_code=proc.returncode, timed_out=False,
        stdout=decode(out or b""), stderr=decode(err or b""),
    )


def format_result(
    result: ProcResult,
    *,
    max_chars: int = MAX_OUTPUT_CHARS,
    redact: tuple[str, ...] = (),
    timeout_seconds: int = 0,
) -> str:
    """把 ProcResult 格式化为给模型看的文本（消毒 + 封顶 + 超时标注）。"""
    stdout = sanitize_output(result.stdout or "", redact)
    stderr = sanitize_output(result.stderr or "", redact)
    merged = stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else "")
    if result.timed_out:
        merged = merged[:max_chars]
        limit = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        brief = f"[执行超时] 命令超过 {limit} 秒被终止（进程树已结束）"
        return f"{brief}。部分输出：\n{merged}" if merged.strip() else brief
    if len(merged) > max_chars:
        merged = merged[:max_chars] + f"\n…（输出超长，已截断至 {max_chars} 字符）"
    status = "exit 0" if result.exit_code == 0 else f"exit {result.exit_code}"
    return f"[{status}]\n{merged}" if merged.strip() else f"[{status}]（无输出）"


__all__ = [
    "BASE_ENV_WHITELIST",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_OUTPUT_CHARS",
    "ProcResult",
    "build_env",
    "decode",
    "format_result",
    "kill_process_tree",
    "run_argv",
    "sanitize_output",
]
