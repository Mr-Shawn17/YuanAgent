from __future__ import annotations

from pathlib import Path

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


def _relative(root: Path, target: Path) -> str:
    try:
        return str(target.relative_to(root))
    except ValueError:
        return str(target)


def make_list_directory_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    root = context.sandbox()

    @tool("list_directory")
    @tool_error_boundary
    def list_directory(path: str = ".") -> str:
        """列出工作沙箱内某个目录下的文件与子目录。

        Args:
            path: 沙箱内的相对路径，默认为沙箱根目录。
        """
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

    return list_directory


def make_read_file_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    root = context.sandbox()

    @tool("read_file")
    @tool_error_boundary
    def read_file(path: str, max_chars: int = DEFAULT_READ_LIMIT) -> str:
        """读取工作沙箱内某个文本文件的内容。

        Args:
            path: 沙箱内的相对路径。
            max_chars: 最多读取的字符数，默认 20000，超出部分会被截断。
        """
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

    return read_file


def make_write_file_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    root = context.sandbox()

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

    return write_file


__all__ = ["make_list_directory_tool", "make_read_file_tool", "make_write_file_tool"]
