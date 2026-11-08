"""依赖安装：固定模板执行（最小集），不做通用命令执行（docs/codex-integration.md §4.2）。

模板探测顺序（auto）：
  uv.lock → uv sync；requirements.txt → pip install -r
  package-lock.json → npm ci；package.json → npm install
- Python 解释器：目标目录 .venv 优先，其次 runner.yaml deps.python，都没有则报错
  （不静默用系统 Python，避免污染用户环境）；
- pip 安装本质会执行构建代码，因此该工具在服务端是 danger 档（用户审批可见命令）；
- 显式 manager 只能从 pip / uv / npm 中选，其余一律拒绝。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from runner.config import RunnerConfig
from runner.executor import resolve_in_sandbox
from runner.procs import format_result, run_argv

ALLOWED_MANAGERS = frozenset({"auto", "pip", "uv", "npm"})


def _venv_python(root: Path) -> Path | None:
    """目标目录下 .venv 里的解释器（Windows Scripts / POSIX bin）。"""
    if sys.platform == "win32":
        candidate = root / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = root / ".venv" / "bin" / "python"
    return candidate if candidate.is_file() else None


def resolve_python(root: Path, config: RunnerConfig) -> str | None:
    """解释器解析：.venv 优先 → deps.python；找不到返回 None。"""
    local = _venv_python(root)
    if local is not None:
        return str(local)
    configured = config.deps.python.strip()
    if configured and Path(configured).is_file():
        return configured
    return None


def detect_template(root: Path, manager: str) -> tuple[str, list[str]] | None:
    """探测依赖清单 → (说明, argv)。manager 非 auto 时只认对应清单。

    argv 中的 {python} 占位符由调用方替换为解析后的解释器路径。
    """
    wants = manager if manager != "auto" else ""
    has_uv_lock = (root / "uv.lock").is_file()
    has_requirements = (root / "requirements.txt").is_file()
    has_package_lock = (root / "package-lock.json").is_file()
    has_package_json = (root / "package.json").is_file()

    if (not wants or wants == "uv") and has_uv_lock:
        uv = shutil.which("uv")
        if uv:
            return "uv sync（uv.lock）", [uv, "sync"]
    if (not wants or wants == "pip") and has_requirements:
        return (
            "pip install -r requirements.txt",
            ["{python}", "-m", "pip", "install", "-r", "requirements.txt"],
        )
    if (not wants or wants == "npm") and has_package_lock:
        npm = shutil.which("npm")
        if npm:
            return "npm ci（package-lock.json）", [npm, "ci"]
    if (not wants or wants == "npm") and has_package_json:
        npm = shutil.which("npm")
        if npm:
            return "npm install（package.json）", [npm, "install"]
    return None


def run_install_deps(workspace: Path, args: dict[str, object], config: RunnerConfig) -> str:
    """install_deps 实现：固定模板装依赖，永不抛异常，返回给模型的文本。"""
    manager = str(args.get("manager", "auto")).strip().lower() or "auto"
    if manager not in ALLOWED_MANAGERS:
        return f"[参数无效] manager 只能是 auto/pip/uv/npm，收到：{manager}"
    path = str(args.get("path", ".") or ".").strip() or "."
    try:
        root = resolve_in_sandbox(workspace, path)
    except Exception:
        return f"[路径越界] path 必须在工作目录内：{path}"
    if not root.is_dir():
        return f"目录不存在：{path}"

    template = detect_template(root, manager)
    if template is None:
        found = []
        if (root / "uv.lock").exists():
            found.append("uv.lock")
        if (root / "requirements.txt").exists():
            found.append("requirements.txt")
        if (root / "package.json").exists():
            found.append("package.json")
        hint = f"发现的清单：{', '.join(found)}" if found else "未发现任何依赖清单"
        return (
            f"[无法安装] 没有匹配的依赖模板（manager={manager}，{hint}）。"
            "支持：uv.lock → uv sync；requirements.txt → pip install -r；"
            "package-lock.json → npm ci；package.json → npm install"
        )

    label, argv = template
    if "{python}" in argv:
        python = resolve_python(root, config)
        if python is None:
            return (
                "[缺少 Python] 未找到工作目录下的 .venv，且 runner.yaml deps.python 未配置。"
                "请先在项目里创建虚拟环境（python -m venv .venv），"
                "或在 runner.yaml 的 deps.python 指定解释器"
            )
        argv = [python if item == "{python}" else item for item in argv]

    result = run_argv(argv, cwd=root, timeout=config.deps.timeout_seconds)
    formatted = format_result(result, timeout_seconds=config.deps.timeout_seconds)
    return f"依赖安装（{label}）：\n{formatted}"


__all__ = [
    "ALLOWED_MANAGERS",
    "detect_template",
    "resolve_python",
    "run_install_deps",
]
