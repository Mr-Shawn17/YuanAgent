from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.tools.base import (
    DEFAULT_READ_LIMIT,
    MAX_LIST_ENTRIES,
    MAX_READ_LIMIT,
    ToolContext,
    ToolEntryConfig,
    resolve_in_sandbox,
    tool_error_boundary,
)

WRITE_MODES = ("overwrite", "append", "delete")

# tools.yaml 中文件工具的可选执行位置：server=服务器沙箱直连，local=转发到用户执行器
FILE_MODES = ("server", "local")


def _wants_local(entry: ToolEntryConfig, context: ToolContext) -> bool:
    """是否启用本地执行器模式：显式 local 且注入了可用 router 才转发。

    CLI 场景不注入 router：即使配置写了 local 也退化为直读本机沙箱
    （CLI 进程本来就在用户电脑上，语义等价）。
    """
    return entry.mode.strip().lower() == "local" and context.local_router is not None


def _relative(root: Path, target: Path) -> str:
    try:
        return str(target.relative_to(root))
    except ValueError:
        return str(target)


def _dispatch_local(
    context: ToolContext,
    *,
    tool_name: str,
    args: dict[str, Any],
) -> str:
    """local 模式转发：同步工具函数内经 asyncio.run 桥接到异步 router。

    同步工具在 Agent 的 ainvoke 下走 run_in_executor 线程池，本函数在
    工作线程执行，没有运行中的事件循环，asyncio.run 安全；CLI 直接
    invoke 亦同。router.dispatch 保证不抛异常，返回可读文本。
    """
    router: Any = context.local_router
    assert router is not None
    return asyncio.run(
        router.dispatch(
            user_id=context.user_id,
            tool=tool_name,
            args=args,
            workspace=context.workspace,
        )
    )


def make_list_directory_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    root = context.sandbox()
    use_local = _wants_local(entry, context)

    @tool("list_directory")
    @tool_error_boundary
    def list_directory(path: str = ".") -> str:
        """列出工作沙箱内某个目录下的文件与子目录。

        Args:
            path: 沙箱内的相对路径，默认为沙箱根目录。
        """
        if use_local:
            return _dispatch_local(context, tool_name="list_directory", args={"path": path})
        return _list_directory_local(root, path)

    return list_directory


def _list_directory_local(root: Path, path: str) -> str:
    target = resolve_in_sandbox(root, path)
    if not target.exists():
        return f"目录不存在：{path}"
    if not target.is_dir():
        return f"不是目录：{path}"

    entries = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
    lines = [
        f"- {item.name}{'/' if item.is_dir() else ''}" for item in entries[:MAX_LIST_ENTRIES]
    ]
    if not lines:
        return f"目录为空：{path}"
    if len(entries) > MAX_LIST_ENTRIES:
        lines.append(f"…（共 {len(entries)} 项，仅显示前 {MAX_LIST_ENTRIES} 项）")
    return f"目录 {path or '.'} 的内容：\n" + "\n".join(lines)


def make_read_file_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    root = context.sandbox()
    use_local = _wants_local(entry, context)

    @tool("read_file")
    @tool_error_boundary
    def read_file(path: str, max_chars: int = DEFAULT_READ_LIMIT) -> str:
        """读取工作沙箱内某个文本文件的内容。

        Args:
            path: 沙箱内的相对路径。
            max_chars: 最多读取的字符数，默认 20000，超出部分会被截断。
        """
        if use_local:
            return _dispatch_local(
                context,
                tool_name="read_file",
                args={"path": path, "max_chars": max_chars},
            )
        return _read_file_local(root, path, max_chars)

    return read_file


def _read_file_local(root: Path, path: str, max_chars: int) -> str:
    target = resolve_in_sandbox(root, path)
    if not target.exists():
        return f"文件不存在：{path}"
    if not target.is_file():
        return f"不是文件：{path}"

    limit = max(1, min(int(max_chars), MAX_READ_LIMIT))
    text = target.read_text(encoding="utf-8", errors="replace")
    if not text:
        return f"文件为空：{path}"
    if len(text) > limit:
        return text[:limit] + f"\n…[内容过长已截断，全文共 {len(text)} 字符]"
    return text


def make_write_file_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    root = context.sandbox()
    use_local = _wants_local(entry, context)

    @tool("write_file")
    @tool_error_boundary
    def write_file(path: str, content: str = "", mode: str = "overwrite") -> str:
        """在工作沙箱内写入、追加或删除文件。属于写操作，执行前需要用户确认。

        Args:
            path: 沙箱内的相对路径。
            content: 要写入的文本内容；mode=delete 时可留空。
            mode: overwrite（覆盖写，默认）/ append（追加）/ delete（删除文件）。
        """
        action = mode.strip().lower()
        if action not in WRITE_MODES:
            return f"不支持的模式：{mode}，可选 overwrite/append/delete"

        if use_local:
            return _dispatch_local(
                context,
                tool_name="write_file",
                args={"path": path, "content": content, "mode": action},
            )
        return _write_file_local(root, path, content, action)

    return write_file


def _write_file_local(root: Path, path: str, content: str, action: str) -> str:
    target = resolve_in_sandbox(root, path)
    if action == "delete":
        if not target.exists():
            return f"文件不存在，无需删除：{path}"
        if target.is_dir():
            return "出于安全考虑只支持删除文件，不支持删除目录"
        target.unlink()
        return f"已删除文件：{_relative(root, target)}"

    target.parent.mkdir(parents=True, exist_ok=True)
    if action == "append":
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
        return f"已追加 {len(content)} 个字符到 {_relative(root, target)}"
    target.write_text(content, encoding="utf-8")
    return f"已写入 {len(content)} 个字符到 {_relative(root, target)}"


__all__ = [
    "FILE_MODES",
    "make_list_directory_tool",
    "make_read_file_tool",
    "make_write_file_tool",
]
