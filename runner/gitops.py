"""结构化 git 工具：幂等拉取（clone/pull）+ 提交推送（add/commit/push）。

命令行由本模块以 argv 列表拼装（无 shell 字符串），模型只提供结构化参数。
安全设计（docs/codex-integration.md §5）：
1. repo_url 仅 https + 主机白名单（runner.yaml git.allowed_hosts）+ 字符白名单，
   拒绝 URL 内嵌凭证；
2. 凭证（username + PAT）存系统凭据管理器（keyring，服务名与设备令牌一致），
   经 GIT_ASKPASS 助手脚本以环境变量注入，绝不拼进 URL / 命令行 / 回传输出；
3. git_commit_push 只推回 clone 时登记的 remote + 分支（workspace/.yuan/remote.json），
   当前仓库 remote 与登记不一致即拒绝——防"推到攻击者仓库"；
4. 全部输出经 sanitize_output 剥离 PAT 与常见 token 模式（双保险）。
第一期仅 HTTPS + PAT；SSH 认证二期。
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path
from urllib.parse import urlparse

from runner.config import GitConfig, RunnerConfig
from runner.executor import resolve_in_sandbox
from runner.procs import ProcResult, format_result, run_argv

GIT_SERVICE_NAME = "YuanAgentRunner"
GIT_ACCOUNT_PREFIX = "git:"

MAX_REPO_URL_CHARS = 500
MAX_COMMIT_MESSAGE_CHARS = 500

# URL 允许的字符（不含引号、管道、重定向、插入符、反引号、空白、反斜杠）
_URL_SAFE_RE = re.compile(r"^[A-Za-z0-9:/._~?&=#,\[\]@+-]+$")
# 分支名允许的字符
_BRANCH_SAFE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

_REGISTRY_FILE = ".yuan/remote.json"
_ASKPASS_DIR = ".yuan/askpass"

_ASKPASS_CMD = """@echo off
echo %1 | findstr /i /c:"Username" >nul
if %errorlevel%==0 (echo %YUAN_GIT_USER%) else (echo %YUAN_GIT_PAT%)
"""
_ASKPASS_SH = """#!/bin/sh
case "$1" in *Username*) echo "$YUAN_GIT_USER";; *) echo "$YUAN_GIT_PAT";; esac
"""


# ---------------------------------------------------------------------------
# 凭据存取（keyring；与 CLI 子命令 set-git-cred / clear-git-cred 共用）
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
    """读取某 host 的凭证；无则 None（公开仓库场景裸跑 git）。"""
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
    """删除某 host 的凭证；不存在时返回 False。"""
    try:
        import keyring

        keyring.delete_password(GIT_SERVICE_NAME, f"{GIT_ACCOUNT_PREFIX}{host.strip().lower()}")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def parse_repo_url(raw: str, allowed_hosts: list[str]) -> tuple[str, str, str]:
    """校验 repo_url → (url, host, repo_name)。非法抛 ValueError（调用方转文本）。

    仅 https；主机必须在白名单内；拒绝 URL 内嵌凭证（user:pass@host）。
    """
    url = raw.strip()
    if not url:
        raise ValueError("repo_url 不能为空")
    if len(url) > MAX_REPO_URL_CHARS:
        raise ValueError(f"repo_url 过长（>{MAX_REPO_URL_CHARS} 字符）")
    if not _URL_SAFE_RE.match(url):
        raise ValueError(f"repo_url 含不允许的字符（仅支持标准 URL 字符集）：{url[:120]}")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("仅支持 https:// 的 git 地址（SSH 二期支持）")
    if parsed.username or parsed.password:
        raise ValueError("repo_url 不允许内嵌凭证（user:pass@host），请用 set-git-cred 录入")
    host = (parsed.hostname or "").lower()
    allowed = {item.strip().lower() for item in allowed_hosts if item.strip()}
    if allowed and host not in allowed:
        raise ValueError(f"仓库主机 {host} 不在白名单内（runner.yaml git.allowed_hosts）")
    repo_name = (parsed.path or "").rstrip("/").split("/")[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[: -len(".git")]
    if not repo_name:
        raise ValueError("无法从地址中解析出仓库名，请用 target_dir 指定目录名")
    return url, host, repo_name


def _validate_branch(branch: str) -> str:
    text = branch.strip()
    if text and not _BRANCH_SAFE_RE.match(text):
        raise ValueError(f"branch 含不允许的字符：{text[:60]}")
    return text


# ---------------------------------------------------------------------------
# 工具实现（永不抛异常，返回给模型的文本）
# ---------------------------------------------------------------------------


def run_git_clone(workspace: Path, args: dict[str, object], config: RunnerConfig) -> str:
    """git_clone：目标目录已是 git 仓库则 pull --ff-only（幂等），否则 clone。"""
    git_cfg = config.git
    askpass: Path | None = None
    try:
        try:
            url, host, repo_name = parse_repo_url(
                str(args.get("repo_url", "")), git_cfg.allowed_hosts
            )
            branch = _validate_branch(str(args.get("branch", "")))
            target = str(args.get("target_dir", "")).strip() or repo_name
            repo_path = resolve_in_sandbox(workspace, target)
        except ValueError as exc:
            return f"[参数无效] {exc}"

        cred = get_git_cred(host)
        extra_env: dict[str, str] | None = None
        redact: tuple[str, ...] = ()
        if cred is not None:
            askpass, extra_env = _write_askpass(workspace, cred[0], cred[1])
            redact = (cred[1],)

        if _is_git_repo(repo_path):
            result = _git(
                ["pull", "--ff-only", "--progress"], cwd=repo_path,
                cfg=git_cfg, extra_env=extra_env,
            )
            action = "已更新（git pull --ff-only）"
        elif repo_path.exists() and any(repo_path.iterdir()):
            return (
                f"[目录冲突] {target} 已存在且不是 git 仓库，请换一个 target_dir "
                "或先清理该目录"
            )
        else:
            argv = ["clone", "--progress"]
            if branch:
                argv += ["--branch", branch]
            if git_cfg.clone_depth > 0:
                argv += ["--depth", str(git_cfg.clone_depth)]
            argv += [url, target]
            result = _git(argv, cwd=workspace, cfg=git_cfg, extra_env=extra_env)
            action = f"已克隆到 {target}"

        if result.exit_code != 0 and not result.timed_out:
            return f"[git 失败] {action}未完成：\n{format_result(result, redact=redact)}"
        _register_remote(workspace, repo_path, url, target, git_cfg)
        return f"{action}。\n{format_result(result, redact=redact)}"
    finally:
        if askpass is not None:
            askpass.unlink(missing_ok=True)


def run_git_commit_push(workspace: Path, args: dict[str, object], config: RunnerConfig) -> str:
    """git_commit_push：add -A → commit → push 登记的 remote+分支。"""
    git_cfg = config.git
    askpass: Path | None = None
    try:
        message = str(args.get("message", "")).strip()
        if not message:
            return "[参数无效] message 不能为空"
        if len(message) > MAX_COMMIT_MESSAGE_CHARS:
            return f"[参数无效] message 过长（>{MAX_COMMIT_MESSAGE_CHARS} 字符），请精简说明"
        registry = _read_remote_registry(workspace)
        if registry is None:
            return "[未登记仓库] 当前工作目录还没有通过 git_clone 拉取过仓库，无法推送"
        try:
            repo_path = resolve_in_sandbox(workspace, str(registry.get("path", "")))
        except Exception:
            return "[登记损坏] remote.json 中的 path 非法，请重新 git_clone"
        if not _is_git_repo(repo_path):
            return "[不是 git 仓库] 登记的目录已不是 git 仓库，请重新 git_clone"

        remote_url = _git_output(
            ["config", "--get", "remote.origin.url"], cwd=repo_path, cfg=git_cfg
        )
        actual_host = (urlparse(remote_url).hostname or "").lower() if remote_url else ""
        if not actual_host or actual_host != str(registry.get("host", "")):
            return (
                f"[拒绝推送] 当前仓库 remote（{remote_url or '未知'}）与 clone 时登记的 "
                f"{registry.get('host', '')} 不一致，疑似被篡改；如确认无误请重新 git_clone"
            )
        branch = str(registry.get("branch", "")).strip()
        if not branch:
            return "[登记损坏] remote.json 缺少 branch，请重新 git_clone"

        cred = get_git_cred(actual_host)
        extra_env: dict[str, str] | None = None
        redact: tuple[str, ...] = ()
        if cred is not None:
            askpass, extra_env = _write_askpass(workspace, cred[0], cred[1])
            redact = (cred[1],)

        _git(["add", "-A"], cwd=repo_path, cfg=git_cfg, extra_env=extra_env)
        status = _git_output(
            ["status", "--porcelain"], cwd=repo_path, cfg=git_cfg, extra_env=extra_env
        )
        if not status.strip():
            return "工作区无改动，跳过提交与推送"

        commit_argv = _commit_argv(message, cwd=repo_path, cfg=git_cfg)
        commit = _git(commit_argv, cwd=repo_path, cfg=git_cfg, extra_env=extra_env)
        if commit.exit_code != 0 and not commit.timed_out:
            return f"[git 失败] 提交未完成：\n{format_result(commit, redact=redact)}"
        push = _git(
            ["push", "--progress", "origin", branch], cwd=repo_path,
            cfg=git_cfg, extra_env=extra_env,
        )
        brief_lines = commit.stdout.strip().splitlines()
        commit_brief = brief_lines[0][:200] if brief_lines else "已提交"
        if push.exit_code != 0 and not push.timed_out:
            return (
                f"[git 失败] 已提交（{commit_brief}）但推送未成功：\n"
                f"{format_result(push, redact=redact)}"
            )
        push_text = format_result(push, redact=redact)
        return f"已提交（{commit_brief}）并推送到 origin/{branch}。\n{push_text}"
    finally:
        if askpass is not None:
            askpass.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 内部助手
# ---------------------------------------------------------------------------


def _git(
    argv: list[str], *, cwd: Path, cfg: GitConfig, extra_env: dict[str, str] | None
) -> ProcResult:
    return run_argv(["git", *argv], cwd=cwd, timeout=cfg.timeout_seconds, extra_env=extra_env)


def _git_output(argv: list[str], *, cwd: Path, cfg: GitConfig,
                extra_env: dict[str, str] | None = None) -> str:
    result = _git(argv, cwd=cwd, cfg=cfg, extra_env=extra_env)
    if result.exit_code != 0 or result.timed_out:
        return ""
    return result.stdout.strip()


def _commit_argv(message: str, *, cwd: Path, cfg: GitConfig) -> list[str]:
    """组装 commit argv；仓库/全局未配置身份时才注入兜底身份（不打扰用户配置）。"""
    argv: list[str] = []
    if not _git_output(["config", "user.name"], cwd=cwd, cfg=cfg):
        argv += ["-c", f'user.name={cfg.user_name}', "-c", f"user.email={cfg.user_email}"]
    return [*argv, "commit", "-m", message]


def _write_askpass(workspace: Path, username: str, pat: str) -> tuple[Path, dict[str, str]]:
    """写临时 askpass 脚本，返回 (脚本路径, 注入环境)。用后由调用方删除。"""
    script_dir = workspace / ".yuan" / "askpass"
    script_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        script = script_dir / f"askpass_{uuid.uuid4().hex[:8]}.cmd"
        script.write_text(_ASKPASS_CMD, encoding="utf-8")
    else:
        script = script_dir / f"askpass_{uuid.uuid4().hex[:8]}.sh"
        script.write_text(_ASKPASS_SH, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    env = {
        "GIT_ASKPASS": str(script),
        "GIT_TERMINAL_PROMPT": "0",
        "YUAN_GIT_USER": username,
        "YUAN_GIT_PAT": pat,
    }
    return script, env


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _register_remote(workspace: Path, repo_path: Path, url: str, target: str,
                     cfg: GitConfig) -> None:
    """clone/pull 成功后登记 remote 信息，供 git_commit_push 校验推送目标。"""
    branch = _git_output(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path, cfg=cfg)
    registry = {
        "url": url,
        "host": (urlparse(url).hostname or "").lower(),
        "branch": branch or "main",
        "path": target,
    }
    path = workspace / _REGISTRY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_remote_registry(workspace: Path) -> dict[str, str] | None:
    try:
        raw = (workspace / _REGISTRY_FILE).read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(key): str(value) for key, value in data.items()}
    except (OSError, ValueError):
        pass
    return None


__all__ = [
    "GIT_ACCOUNT_PREFIX",
    "GIT_SERVICE_NAME",
    "MAX_COMMIT_MESSAGE_CHARS",
    "MAX_REPO_URL_CHARS",
    "delete_git_cred",
    "get_git_cred",
    "parse_repo_url",
    "run_git_clone",
    "run_git_commit_push",
    "set_git_cred",
]
