"""工作目录扫描：枚举本机常用目录，供网页下拉选择。

只扫描固定的常用位置（主目录、桌面、文档、下载等），不做任意目录的
递归扫描——避免性能开销与隐私问题。用户手填的目录由 set-workspace 校验。

返回列表按 kind 排序，去重（同一路径只保留一次）。
"""

from __future__ import annotations

import os
from pathlib import Path

from runner.protocol import WorkspaceInfo

# 已知目录标识名（中文展示）
_KIND_LABELS: dict[str, str] = {
    "home": "主目录",
    "desktop": "桌面",
    "documents": "文档",
    "downloads": "下载",
    "pictures": "图片",
    "code": "代码",
}

# 常见代码目录名（扫描主目录下这些名字的子目录）
_CODE_DIR_NAMES = ("code", "Code", "dev", "Dev", "projects", "Projects", "workspace", "src")


def _known_path(kind: str) -> Path | None:
    """按 kind 取系统已知目录（跨平台回退）。"""
    if kind == "home":
        return Path.home()
    if kind == "desktop":
        home = Path.home()
        desktop = home / "Desktop"
        return desktop if desktop.exists() else home / "桌面"
    if kind == "documents":
        home = Path.home()
        docs = home / "Documents"
        return docs if docs.exists() else home / "文档"
    if kind == "downloads":
        home = Path.home()
        dl = home / "Downloads"
        return dl if dl.exists() else home / "下载"
    if kind == "pictures":
        home = Path.home()
        pic = home / "Pictures"
        return pic if pic.exists() else home / "图片"
    return None


def _code_dirs() -> list[Path]:
    """主目录下常见代码目录（存在才收录，最多几个）。"""
    home = Path.home()
    found: list[Path] = []
    for name in _CODE_DIR_NAMES:
        candidate = home / name
        if candidate.is_dir() and candidate not in found:
            found.append(candidate)
        if len(found) >= 4:
            break
    return found


def scan_workspaces() -> list[WorkspaceInfo]:
    """扫描本机常用目录，返回 WorkspaceInfo 列表（去重、按 kind 排序）。"""
    items: list[WorkspaceInfo] = []
    seen: set[str] = set()

    def _add(path: Path, kind: str) -> None:
        if not path.is_dir():
            return
        resolved = str(path.resolve())
        if resolved in seen:
            return
        seen.add(resolved)
        name = _KIND_LABELS.get(kind, path.name or resolved)
        items.append(WorkspaceInfo(path=resolved, name=name, kind=kind))

    for kind in ("home", "desktop", "documents", "downloads", "pictures"):
        path = _known_path(kind)
        if path is not None:
            _add(path, kind)
    for path in _code_dirs():
        _add(path, "code")
    return items


def workspace_from_env() -> Path | None:
    """从环境变量 YUAN_WORKSPACE 取默认工作目录（可选覆盖）。"""
    raw = os.environ.get("YUAN_WORKSPACE", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path.resolve() if path.is_dir() else None


__all__ = ["scan_workspaces", "workspace_from_env"]
