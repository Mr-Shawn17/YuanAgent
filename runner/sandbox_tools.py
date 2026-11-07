"""VM 档便携工具链管理：下载/解压 git、python、node 到宿主工具目录。

Windows Sandbox 是干净的一次性系统，宿主已装的工具链沙箱内不可见——
首次使用 VM 档时把便携版工具下载到 %LOCALAPPDATA%\\YuanAgent\\sandbox-tools\\
（一次性约 300~500MB），.wsb 以只读方式映射进沙箱并注入 PATH。

- 下载源默认走 npmmirror（国内可达；可改常量或手动放置）
- 每个工具一个子目录 + .ready 标记（用户也可手动解压工具到子目录并自建 .ready 跳过下载）
- git 为 PortableGit 自解压包（静默 -o<dir> -y）；python/node 为 zip（标准库解压）
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from runner.config import app_dir

# 工具清单：名称 → (下载 URL, 安装方式)。升级版本改这里。
# npmmirror 镜像了 git-for-windows 与 node 官方发布（本机直连 github 不可达时的默认选择）。
TOOL_SOURCES: dict[str, tuple[str, str]] = {
    "git": (
        "https://registry.npmmirror.com/-/binary/git-for-windows/"
        "v2.47.1.windows.1/PortableGit-2.47.1-64-bit.7z.exe",
        "selfextract",
    ),
    "python": (
        "https://registry.npmmirror.com/-/binary/python/"
        "3.12.7/python-3.12.7-embed-amd64.zip",
        "zip",
    ),
    "node": (
        "https://registry.npmmirror.com/-/binary/node/"
        "v22.14.0/node-v22.14.0-win-x64.zip",
        "zip_nested",
    ),
}

DOWNLOAD_TIMEOUT = 600
_CHUNK = 1024 * 256


def tools_root(custom: str = "") -> Path:
    root = Path(custom).expanduser() if custom.strip() else app_dir() / "sandbox-tools"
    root.mkdir(parents=True, exist_ok=True)
    return root


def tool_ready(root: Path, name: str) -> bool:
    return (root / name / ".ready").is_file()


def _download(url: str, dest: Path) -> None:
    """流式下载（打印进度行，失败抛异常）。"""
    print(f"  下载 {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "YuanRunner/1.0"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, dest.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                print(f"\r  {pct}%（{done // 1024 // 1024}MB）", end="", flush=True)
    print(flush=True)


def _install(url: str, kind: str, target: Path) -> None:
    """下载并安装一个工具到 target 子目录，完成后写 .ready 标记。"""
    target.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".download-{target.name}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "selfextract":
            exe = tmp / "installer.exe"
            _download(url, exe)
            # PortableGit 自解压：-o 输出目录 -y 静默确认（目标为空目录，无覆盖风险）
            result = subprocess.run(
                [str(exe), "-o", str(target), "-y"],
                capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT, check=False,
            )
            if result.returncode != 0 or not (target / "cmd" / "git.exe").exists():
                msg = f"PortableGit 自解压失败：{result.stderr[:200]}"
                raise RuntimeError(msg)
        else:
            archive = tmp / "archive.zip"
            _download(url, archive)
            extract_to = tmp / "extracted"
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract_to)
            if kind == "zip":
                inner = extract_to  # python embeddable：zip 内容即目标
            else:  # zip_nested：node 的 zip 内有一层 node-vX-win-x64/
                nested = next(extract_to.iterdir(), None)
                if nested is None or not nested.is_dir():
                    raise RuntimeError("node 压缩包结构与预期不符")
                inner = nested
            for item in inner.iterdir():
                shutil.move(str(item), str(target / item.name))
        (target / ".ready").write_text("ok", encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure_tools(names: tuple[str, ...] = ("git",), custom_root: str = "") -> Path:
    """确保指定工具就绪（缺哪个装哪个），返回工具根目录。

    抛出的异常由调用方（WindowsSandboxBackend）转为错误文本返回模型。
    """
    root = tools_root(custom_root)
    for name in names:
        if name not in TOOL_SOURCES:
            raise ValueError(f"未知的沙箱工具：{name}（可选：{sorted(TOOL_SOURCES)}）")
        if tool_ready(root, name):
            continue
        url, kind = TOOL_SOURCES[name]
        print(f"[sandbox-tools] 准备 {name}（首次使用需下载，一次性）", flush=True)
        _install(url, kind, root / name)
        print(f"[sandbox-tools] {name} 就绪：{root / name}", flush=True)
    return root


if __name__ == "__main__":
    # 手动预下载：python -m runner.sandbox_tools [git|python|node ...]
    wanted = tuple(sys.argv[1:]) or ("git",)
    ensure_tools(wanted)
