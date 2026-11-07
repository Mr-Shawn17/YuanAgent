"""结构化 git 工具：clone / pull / push 的命令由 runner 构造，凭证永不外泄。

安全设计：
1. LLM 只提供 repo_url / branch / target_dir / path 等参数，命令行文本由本模块拼装
2. 凭证（username + PAT）存 Windows 凭据管理器（keyring，服务名与设备令牌一致），
   经 GIT_ASKPASS 助手脚本以环境变量注入，绝不拼进 URL / 命令行文本 / 回传输出
3. URL 字符白名单校验（标准 URL 字符，禁引号/管道/插入符），防 shell 注入
4. 输出经 format_result 剥离 PAT（双保险）
第一期仅支持 HTTPS + PAT；SSH key 认证二期。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from runner.command import CommandBackend, CommandPolicy, format_result
from runner.executor import resolve_in_sandbox

GIT_SERVICE_NAME = "YuanAgentRunner"
GIT_ACCOUNT_PREFIX = "git:"

# URL 允许的字符（不含引号、管道、重定向、插入符、反引号、百分号）
_URL_SAFE_RE = re.compile(r"^[A-Za-z0-9:/._~?&=#,\[\]@+-]+$")
# 分支名允许的字符
_BRANCH_SAFE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

_ASKPASS_DIR = ".yuan/askpass"

_ASKPASS_TEMPLATE = """@echo off
echo %1 | findstr /i /c:"Username" >nul
if %errorlevel%==0 (echo %YUAN_GIT_USER%) else (echo %YUAN_GIT_PAT%)
"""


# ---------------------------------------------------------------------------
# 凭据存取（keyring）
# ---------------------------------------------------------------------------


def set_git_cred(host: str, username: str, pat: str) -> None:
    """保存某 git host 的 HTTPS 凭证到系统凭据库。"""
    host = host.strip().lower()
    if not host or not username.strip() or not pat.strip():
        raise ValueError("host / username / PAT 均不能为空")
    import keyring

    keyring.set_password(
        GIT_SERVICE_NAME, f"{GIT_ACCOUNT_PREFIX}{host}",
        json.dumps({"username": username.strip(), "pat": pat.strip()}),
    )


def get_git_cred(host: str) -> tuple[str, str] | None:
    """读取某 host 的凭证；无则 None（公开仓库场景）。"""
    try:
        import keyring

        raw = keyring.get_password(GIT_SERVICE_NAME, f"{GIT_ACCOUNT_PREFIX}{host.strip().lower()}")
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return str(data["username"]), str(data["pat"])
    except (ValueError, KeyError, TypeError):
        return None


def delete_git_cred(host: str) -> bool:
    try:
        import keyring

        keyring.delete_password(GIT_SERVICE_NAME, f"{GIT_ACCOUNT_PREFIX}{host.strip().lower()}")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def _parse_repo_url(raw: str) -> tuple[str, str, str]:
    """校验并解析 repo_url → (url, host, repo_name)。非法直接抛 ValueError。"""
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


def _write_askpass(workspace: Path, username: str, pat: str) -> tuple[Path, dict[str, str]]:
    """在 workspace/.yuan/askpass/ 下写临时 askpass 脚本，返回 (脚本路径, 注入环境)。"""
    import uuid

    script_dir = workspace / ".yuan" / "askpass"
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


def _identity_flags(policy: CommandPolicy) -> str:
    name = policy.git_user_name.replace('"', "")
    email = policy.git_user_email.replace('"', "")
    return f'-c user.name="{name}" -c user.email="{email}"'


async def run_git_tool(
    tool: str,
    args: dict[str, object],
    workspace: Path,
    policy: CommandPolicy,
    backend: CommandBackend,
) -> str:
    """git_clone / git_pull / git_push 统一实现。参数已消毒，命令由本函数拼装。"""
    redact: tuple[str, ...] = ()
    askpass_script: Path | None = None
    try:
        extra_env: dict[str, str] | None = None
        if tool == "git_clone":
            url, host, repo_name = _parse_repo_url(str(args.get("repo_url", "")))
            cred = get_git_cred(host)
            command, cwd, timeout = _build_clone(url, repo_name, args, workspace, policy)
        else:
            command, cwd, timeout = _build_pull_push(tool, args, workspace, policy)
            cred = await _cred_from_remote(backend, cwd) or get_git_cred(
                str(args.get("host", "")).strip().lower()
            )
        if cred is not None:
            askpass_script, extra_env = _write_askpass(workspace, cred[0], cred[1])
            redact = (cred[1],)
        result = await backend.run(
            command, cwd=cwd, timeout=timeout, extra_env=extra_env, workspace=workspace
        )
        return format_result(
            result,
            max_chars=policy.max_output_chars,
            redact=redact,
            timeout_seconds=timeout,
        )
    finally:
        if askpass_script is not None:
            askpass_script.unlink(missing_ok=True)


async def _cred_from_remote(backend: CommandBackend, cwd: Path) -> tuple[str, str] | None:
    """pull/push：从 remote.origin.url 推断 host 再查凭证；探测失败返回 None。

    无凭证时裸跑 git，由 git 自身的凭证缓存（如系统凭据管理器）兜底。
    """
    probe = await backend.run(
        "git config --get remote.origin.url", cwd=cwd, timeout=10
    )
    if probe.timed_out or probe.exit_code != 0:
        return None
    lines = probe.stdout.strip().splitlines()
    url = lines[0].strip() if lines else ""
    if not url:
        return None
    if url.startswith("git@"):  # ssh 形式 git@host:path
        host = url.split("@", 1)[1].split(":", 1)[0].lower()
    else:
        host = (urlparse(url).netloc or "").lower()
    return get_git_cred(host) if host else None


def _git_timeout(policy: CommandPolicy, raw: object) -> int:
    value = int(raw or policy.timeout_seconds)
    return max(1, min(value, policy.max_timeout_seconds))


def _build_clone(
    url: str,
    repo_name: str,
    args: dict[str, object],
    workspace: Path,
    policy: CommandPolicy,
) -> tuple[str, Path, int]:
    branch = str(args.get("branch", "")).strip()
    if branch and not _BRANCH_SAFE_RE.match(branch):
        raise ValueError(f"branch 含不允许的字符：{branch[:60]}")
    target = str(args.get("target_dir", "")).strip() or repo_name
    resolve_in_sandbox(workspace, target)  # 越界预检（异常由错误边界转文本）
    timeout = _git_timeout(policy, args.get("timeout_seconds"))
    branch_flag = f" --branch {branch}" if branch else ""
    command = f"git clone{branch_flag} --progress {url} {target}"
    return command, workspace, timeout


def _build_pull_push(
    tool: str, args: dict[str, object], workspace: Path, policy: CommandPolicy
) -> tuple[str, Path, int]:
    path = str(args.get("path", ".") or ".").strip() or "."
    cwd = resolve_in_sandbox(workspace, path)
    timeout = _git_timeout(policy, args.get("timeout_seconds"))
    verb = "pull" if tool == "git_pull" else "push"
    command = f"git {_identity_flags(policy)} {verb} --progress"
    return command, cwd, timeout


__all__ = [
    "delete_git_cred",
    "get_git_cred",
    "run_git_tool",
    "set_git_cred",
]
