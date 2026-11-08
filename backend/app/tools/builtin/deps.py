"""install_deps 工具：按项目依赖清单装依赖（固定模板，docs/codex-integration.md §4.2）。

模板在用户本机 runner 侧执行（runner/deps.py）：uv.lock → uv sync；
requirements.txt → pip install -r；package-lock.json → npm ci；
package.json → npm install。不允许任意包名/自由命令。
pip 与 npm 安装过程会执行构建代码，故保持 danger 档（审批弹窗可见完整命令）。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool, tool

from app.tools.base import ToolContext, ToolEntryConfig, tool_error_boundary
from app.tools.local_dispatch import dispatch_local, wants_local

ALLOWED_MANAGERS = ("auto", "pip", "uv", "npm")

_LOCAL_REQUIRED = (
    "[local_runner_required] 依赖安装需在网页端配合本地执行器（YuanRunner）使用"
)


def make_install_deps_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    use_local = wants_local(entry, context)

    @tool("install_deps")
    @tool_error_boundary
    def install_deps(path: str = ".", manager: str = "auto") -> str:
        """按项目依赖清单安装依赖（固定模板，不装任意包）。
        pip/npm 安装过程会执行构建代码，属于危险操作，执行前需要用户确认。

        Args:
            path: 项目目录在工作目录内的相对路径（存放依赖清单的地方），默认当前目录。
            manager: auto（默认，按 uv.lock / requirements.txt / package*.json
                自动探测）/ pip / uv / npm。
        """
        choice = manager.strip().lower() or "auto"
        if choice not in ALLOWED_MANAGERS:
            return f"[参数无效] manager 只能是 {'/'.join(ALLOWED_MANAGERS)}，收到：{manager}"
        target = path.strip() or "."
        # 服务器侧轻量越界预检（包含检查由 runner 侧二次执行）
        segments = target.replace("\\", "/").split("/")
        if target != "." and (".." in segments or Path(target).is_absolute()):
            return f"[路径越界] path 必须是工作目录内的相对路径：{path}"
        if not use_local:
            return _LOCAL_REQUIRED
        return dispatch_local(
            context,
            tool_name="install_deps",
            args={"path": target, "manager": choice},
            timeout=context.settings.local_devtools_dispatch_timeout,
        )

    return install_deps


__all__ = ["ALLOWED_MANAGERS", "make_install_deps_tool"]
