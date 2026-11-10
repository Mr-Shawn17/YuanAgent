"""RepoSource（spec docs/sandbox-query.md §4）：公开仓库只读副本（Mirror）的供给者。

- MVP = 匿名 clone 公开仓库：``https://github.com/<owner>/<repo>.git``，URL 前缀可配
  （``repo.clone_url_prefix``，国内不稳时配 ``https://ghfast.top/``）；零凭证，
  GIT_TERMINAL_PROMPT=0 + 空 credential.helper 保证永不弹凭据交互。
- 缓存位置 = 双路径（用户 2026-11-10 决策 A）：mirror_root 为 backend 可见路径
  （clone/fetch 目标），mirror_engine_root 为 Engine 侧路径（沙箱 --mount src 前缀）。
  生产 compose：backend rw 挂载 /var/lib/yuan-mirrors 到 /app/data/workspace/mirrors；
  Windows dev：mirror_root 配 \\wsl.localhost UNC 直写 Engine 侧目录。
- 新鲜度 = TTL：.git/FETCH_HEAD mtime 距今超过 mirror_ttl_seconds → ``git fetch
  --all --prune``（预算 120s，spec §7），缺失 → clone。
- 预留接口（route c，私有需求出现再实现）：协议只有 ``resolve(repo_ref) -> Path``，
  runner-bundle 实现经现有 WSS 通道推送 git bundle，凭证全程留边缘。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app.core.config import PROJECT_ROOT, Settings
from app.core.errors import AppError

FETCH_BUDGET_SECONDS = 120  # RepoSource clone/fetch 独立预算（spec §7）
_REPO_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_REPO_REF_CHARS = 200
_GITHUB_URL = "https://github.com/{owner}/{repo}.git"


class RepoSourceError(AppError):
    """镜像获取失败（clone/fetch/路径非法）。code: mirror_unavailable"""

    code = "mirror_unavailable"
    status_code = 502


# 每 (镜像根, owner/repo) 一把进程内锁：并发查询同一仓库时避免同时 clone 写坏目录
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _repo_lock(mirror_dir: Path) -> threading.Lock:
    key = str(mirror_dir)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def parse_repo_ref(repo_ref: str) -> tuple[str, str]:
    """把 "owner/repo" 解析成二元组；拒绝空段、路径穿越与多余分隔。"""
    text = repo_ref.strip().removesuffix(".git")
    if not text:
        raise RepoSourceError("repo_ref 不能为空（应为 GitHub owner/repo）")
    if len(text) > _MAX_REPO_REF_CHARS:
        raise RepoSourceError(f"repo_ref 过长（>{_MAX_REPO_REF_CHARS} 字符）")
    if not _REPO_REF_RE.fullmatch(text):
        raise RepoSourceError(
            f"repo_ref 格式非法：{repo_ref}（应为 owner/repo，"
            "仅允许字母数字与 . _ -，拒绝路径分隔与穿越）"
        )
    owner, _, repo = text.partition("/")
    return owner, repo


def _git_env() -> dict[str, str]:
    """git 子进程环境：透传现有环境（保留代理变量），强制无凭据交互。"""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"  # 匿名 clone：绝不弹终端凭据提示
    return env


def _run_git(argv: list[str], *, cwd: Path | None = None) -> tuple[str, str]:
    """执行一条 git 命令（无 shell），非零退出/超时/缺可执行文件一律抛 RepoSourceError。"""
    # -c credential.helper=：清空本次调用的凭据助手，404/私有库立即失败而非弹窗挂起
    prefixed = ["git", "-c", "credential.helper=", *argv]
    try:
        proc = subprocess.run(
            prefixed,
            cwd=None if cwd is None else str(cwd),
            capture_output=True,
            timeout=FETCH_BUDGET_SECONDS,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        brief = " ".join((exc.cmd or prefixed)[:2])
        raise RepoSourceError(f"git 命令超时（>{FETCH_BUDGET_SECONDS}s）：{brief}") from exc
    except FileNotFoundError as exc:
        raise RepoSourceError(
            "git 可执行文件不可用（生产 backend 镜像需加装 git，见 spec §12）"
        ) from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        brief = " ".join(prefixed[:4])
        tail = stderr[-200:] if stderr else "（无 stderr）"
        raise RepoSourceError(f"git 命令失败（exit {proc.returncode}）：{brief}…\n{tail}")
    return (
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


class RepoSource:
    """镜像来源：resolve(repo_ref) → backend 侧只读副本路径（spec §4 协议）。"""

    def __init__(self, settings: Settings) -> None:
        cfg = settings.sandbox_query
        self.root = self._resolve_mirror_root(settings, cfg.mirror_root)
        # Engine 侧路径：空 = 回退 mirror_root（backend 与 Engine 同文件系统的场景）
        engine_raw = cfg.mirror_engine_root.strip()
        self.engine_root = engine_raw if engine_raw else str(self.root)
        self.clone_url_prefix = cfg.clone_url_prefix.strip()
        self.ttl_seconds = cfg.mirror_ttl_seconds

    @staticmethod
    def _resolve_mirror_root(settings: Settings, raw: str) -> Path:
        """mirror_root：空 = workspace/mirrors（compose 生产布局），相对 = 项目根，绝对原样。"""
        text = raw.strip()
        if not text:
            return settings.workspace_dir / "mirrors"
        path = Path(text)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    # ---- 路径推导 ----------------------------------------------------------

    def mirror_dir(self, repo_ref: str) -> Path:
        """backend 侧副本目录：<mirror_root>/<owner>/<repo>/（spec §4 目录布局）。"""
        owner, repo = parse_repo_ref(repo_ref)
        return self.root / owner / repo

    def engine_mount_src(self, repo_ref: str) -> str:
        """Engine 侧挂载源（沙箱 --mount src 前缀，决策 A 双路径字段）。"""
        mirror_dir = self.mirror_dir(repo_ref)
        if self.engine_root == str(self.root):
            return str(mirror_dir)
        relative = mirror_dir.relative_to(self.root).as_posix()
        return f"{self.engine_root.rstrip('/')}/{relative}"

    # ---- 获取与刷新 --------------------------------------------------------

    def resolve(self, repo_ref: str) -> Path:
        """确保副本存在且新鲜（TTL 内），返回 backend 侧路径。失败抛 RepoSourceError。"""
        mirror_dir = self.mirror_dir(repo_ref)
        with _repo_lock(mirror_dir):
            if (mirror_dir / ".git").is_dir():
                if self._is_fresh(mirror_dir):
                    return mirror_dir
                _run_git(["fetch", "--all", "--prune"], cwd=mirror_dir)
                return mirror_dir
            if mirror_dir.exists():
                # 半成品目录（clone 中断残留）：清掉重 clone。目录属服务端自管缓存。
                shutil.rmtree(mirror_dir)
            mirror_dir.mkdir(parents=True, exist_ok=True)
            _run_git(["clone", "--", self._clone_url(repo_ref), str(mirror_dir)])
            return mirror_dir

    def _is_fresh(self, mirror_dir: Path) -> bool:
        """新鲜度标记 = .git/FETCH_HEAD 的 mtime（clone 与 fetch 都会写它）。"""
        marker = mirror_dir / ".git" / "FETCH_HEAD"
        if not marker.exists():
            return False
        age = time.time() - marker.stat().st_mtime
        return age < self.ttl_seconds

    def _clone_url(self, repo_ref: str) -> str:
        owner, repo = parse_repo_ref(repo_ref)
        github_url = _GITHUB_URL.format(owner=owner, repo=repo)
        if self.clone_url_prefix:
            return f"{self.clone_url_prefix.rstrip('/')}/{github_url}"
        return github_url


__all__ = [
    "FETCH_BUDGET_SECONDS",
    "RepoSource",
    "RepoSourceError",
    "parse_repo_ref",
]
