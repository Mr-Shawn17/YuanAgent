"""结构化 git 工具：git_clone / git_pull / git_push（danger 档）。

私有仓库认证设计（与 runner/gitops.py 同规则，改动双向同步）：
- LLM 只提供 repo_url / branch / target_dir / path，命令文本由本模块拼装
- 凭证（username + PAT）存系统凭据库（keyring，服务名与 YuanRunner 一致），
  经 GIT_ASKPASS 助手脚本以环境变量注入；不进 URL、不进命令行、不进回传输出
- URL 字符白名单校验防 shell 注入；输出对 PAT 做剥离（双保险）
凭证录入：python -m runner set-git-cred <host>（CLI 与 runner 同机共享凭据库）。

git 的其他操作（add/commit/status/log/diff 等）不需要凭证，模型用 run_command 执行。
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

from langchain_core.tools import BaseTool, tool

from app.tools.base import (
    ToolContext,
    ToolEntryConfig,
    resolve_in_sandbox,
    tool_error_boundary,
)
from app.tools.builtin.shell import dispatch_local, run_command_core

GIT_TIMEOUT_SECONDS = 300

_URL_SAFE_RE = re.compile(r"^[A-Za-z0-9:/._~?&=#,\[\]@+-]+$")
_BRANCH_SAFE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_ASKPASS_TEMPLATE = """@echo off
echo %1 | findstr /i /c:"Username" >nul
if %errorlevel%==0 (echo %YUAN_GIT_USER%) else (echo %YUAN_GIT_PAT%)
"""


def _wants_local(entry: ToolEntryConfig, context: ToolContext) -> bool:
    return entry.mode.strip().lower() == "local" and context.local_router is not None


# ---------------------------------------------------------------------------
# CLI 模式的本地实现（与 runner/gitops.py 同规则）
# ---------------------------------------------------------------------------


def _get_git_cred(host: str) -> tuple[str, str] | None:
    try:
        import keyring
    except ImportError:
        return None
    try:
        raw = keyring.get_password("YuanAgentRunner", f"git:{host.strip().lower()}")
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return str(data["username"]), str(data["pat"])
    except (ValueError, KeyError, TypeError):
        return None


def _parse_repo_url(raw: str) -> tuple[str, str, str]:
    url = raw.strip()
    if not url:
        raise ValueError("repo_url 不能为空")
    if not _URL_SAFE_RE.match(url):
        raise ValueError(f"repo_url 含不允许的字符（仅支持标准 URL 字符集）：{url[:120]}")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("第一期仅支持 https:// 或 http:// 的 git 地址（SSH 二期支持）")
    repo_name = (parsed.path or "").rstrip("/").split("/")[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[: -len(".git")]
    if not repo_name:
        raise ValueError("无法从地址中解析出仓库名，请直接指定 target_dir")
    return url, (parsed.netloc or "").lower(), repo_name


def _write_askpass(root: Path, username: str, pat: str) -> tuple[Path, dict[str, str]]:
    script_dir = root / ".yuan" / "askpass"
    script_dir.mkdir(parents=True, exist_ok=True)
    script = script_dir / f"askpass_{uuid.uuid4().hex[:8]}.cmd"
    script.write_text(_ASKPASS_TEMPLATE, encoding="utf-8")
    env = {
        "GIT_ASKPASS": str(script),
        "GIT_TERMINAL_PROMPT": "0",
        "YUAN_GIT_USER": username,
        "YUAN_GIT_PAT": pat,
    }
    return script, env


def _run_with_cred(command: str, cwd: Path, host: str, root: Path) -> str:
    """带凭证执行一条可信 git 命令（凭证注入 + PAT 剥离 + askpass 清理）。"""
    cred = _get_git_cred(host)
    if cred is None:
        return run_command_core(command, cwd=cwd, timeout=GIT_TIMEOUT_SECONDS)
    script, env = _write_askpass(root, cred[0], cred[1])
    try:
        return run_command_core(
            command, cwd=cwd, timeout=GIT_TIMEOUT_SECONDS, redact=(cred[1],), env_extra=env
        )
    finally:
        script.unlink(missing_ok=True)


def _clone_local(repo_url: str, branch: str, target_dir: str, root: Path) -> str:
    url, host, repo_name = _parse_repo_url(repo_url)
    if branch and not _BRANCH_SAFE_RE.match(branch):
        return f"[工具执行失败] branch 含不允许的字符：{branch[:60]}"
    target = str(target_dir).strip() or repo_name
    resolve_in_sandbox(root, target)  # 越界预检（异常由错误边界转文本）
    branch_flag = f" --branch {branch}" if branch else ""
    command = f"git clone{branch_flag} --progress {url} {target}"
    return _run_with_cred(command, root, host, root)


def _pull_push_local(verb: str, path: str, root: Path) -> str:
    cwd = resolve_in_sandbox(root, path or ".")
    cred_host = ""
    # 探测 remote host（可信命令，无凭证需求）
    probe = run_command_core(
        "git config --get remote.origin.url", cwd=cwd, timeout=15
    )
    first = probe.strip().splitlines()[0] if probe.strip() else ""
    if first.startswith(("https://", "http://")):
        cred_host = (urlparse(first).netloc or "").lower()
    command = "git pull --progress" if verb == "pull" else "git push --progress"
    if cred_host:
        return _run_with_cred(command, cwd, cred_host, root)
    return run_command_core(command, cwd=cwd, timeout=GIT_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------


def make_git_clone_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    root = context.sandbox()
    use_local = _wants_local(entry, context)

    @tool("git_clone")
    @tool_error_boundary
    def git_clone(repo_url: str, branch: str = "", target_dir: str = "") -> str:
        """把远程仓库克隆到工作目录（私有仓库凭证由用户预先录入，无需模型经手）。
        属于危险操作，执行前需要用户确认。

        Args:
            repo_url: 仓库地址（https:// 开头；私有仓库需用户先执行
                python -m runner set-git-cred <host> 录入凭证）。
            branch: 要克隆的分支，默认远端默认分支。
            target_dir: 工作目录内的目标文件夹名，默认取仓库名。
        """
        if use_local:
            return dispatch_local(
                context,
                tool_name="git_clone",
                args={"repo_url": repo_url, "branch": branch, "target_dir": target_dir},
                timeout=GIT_TIMEOUT_SECONDS + 45.0,
            )
        return _clone_local(repo_url, branch, target_dir, root)

    return git_clone


def make_git_pull_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    root = context.sandbox()
    use_local = _wants_local(entry, context)

    @tool("git_pull")
    @tool_error_boundary
    def git_pull(path: str = ".") -> str:
        """在工作目录的某个仓库里执行 git pull（拉取远端更新）。
        属于危险操作，执行前需要用户确认。

        Args:
            path: 仓库在工作目录内的相对路径，默认为工作目录根。
        """
        if use_local:
            return dispatch_local(
                context, tool_name="git_pull", args={"path": path},
                timeout=GIT_TIMEOUT_SECONDS + 45.0,
            )
        return _pull_push_local("pull", path, root)

    return git_pull


def make_git_push_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    root = context.sandbox()
    use_local = _wants_local(entry, context)

    @tool("git_push")
    @tool_error_boundary
    def git_push(path: str = ".") -> str:
        """在工作目录的某个仓库里执行 git push（提交需先用 run_command 完成
        git add / git commit）。属于危险操作，执行前需要用户确认。

        Args:
            path: 仓库在工作目录内的相对路径，默认为工作目录根。
        """
        if use_local:
            return dispatch_local(
                context, tool_name="git_push", args={"path": path},
                timeout=GIT_TIMEOUT_SECONDS + 45.0,
            )
        return _pull_push_local("push", path, root)

    return git_push


__all__ = [
    "make_git_clone_tool",
    "make_git_pull_tool",
    "make_git_push_tool",
]
