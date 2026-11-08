"""执行器：文件三件套 + 开发工具四件套（git/依赖/codex 委派）+ 沙箱越界检查。

与主项目 backend/app/tools/base.py 的 resolve_in_sandbox 同款规则：
拼接沙箱根后 resolve，is_relative_to 包含检查拒绝 ../ 与绝对路径逃逸。
工具白名单硬编码，未知工具名直接拒绝（中心侧新增工具不会静默穿透）。
开发工具的命令由 runner/gitops|deps|coding.py 固定模板拼装（docs/codex-integration.md）。
"""

from __future__ import annotations

from pathlib import Path

from runner.config import RunnerConfig

MAX_LIST_ENTRIES = 200
DEFAULT_READ_LIMIT = 20_000
MAX_READ_LIMIT = 200_000
WRITE_MODES = ("overwrite", "append", "delete")
FILE_TOOLS = frozenset({"read_file", "write_file", "list_directory"})
DEV_TOOLS = frozenset({"git_clone", "git_commit_push", "install_deps", "delegate_coding"})
ALLOWED_TOOLS = FILE_TOOLS | DEV_TOOLS


class SandboxEscapeError(Exception):
    """路径越出沙箱（拒绝执行）。"""


def resolve_in_sandbox(root: Path, path: str) -> Path:
    """把请求路径解析到沙箱内；任何形式的越界抛 SandboxEscapeError。"""
    raw = path.strip()
    if not raw:
        raise SandboxEscapeError("文件路径不能为空")
    resolved_root = root.resolve()
    candidate = (resolved_root / raw).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise SandboxEscapeError(f"禁止访问沙箱之外的路径：{path}")
    return candidate


def _relative(root: Path, target: Path) -> str:
    try:
        return str(target.relative_to(root))
    except ValueError:
        return str(target)


def list_directory(root: Path, path: str) -> str:
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


def read_file(root: Path, path: str, max_chars: int = DEFAULT_READ_LIMIT) -> str:
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


def write_file(root: Path, path: str, content: str, mode: str = "overwrite") -> str:
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


def execute_tool(
    root: Path, tool: str, args: dict[str, object], config: RunnerConfig | None = None
) -> str:
    """统一入口：白名单校验 + 分发。异常不在此捕获（调用方统一转错误结果）。

    开发工具需要 config（connection 接线时传入）；缺失时返回可读错误而非崩溃。
    """
    if tool not in ALLOWED_TOOLS:
        raise ValueError(f"工具不在执行器白名单内：{tool}")
    if tool in DEV_TOOLS:
        # 延迟导入：gitops/deps/coding 反向依赖本模块的 resolve_in_sandbox，顶层互导成环
        if config is None:
            return "[配置缺失] 开发工具需要 RunnerConfig（执行器接线异常）"
        if tool == "git_clone":
            from runner.gitops import run_git_clone

            return run_git_clone(root, args, config)
        if tool == "git_commit_push":
            from runner.gitops import run_git_commit_push

            return run_git_commit_push(root, args, config)
        if tool == "install_deps":
            from runner.deps import run_install_deps

            return run_install_deps(root, args, config)
        from runner.coding import run_codex_task

        return run_codex_task(args, root, config)
    if tool == "read_file":
        return read_file(
            root,
            str(args.get("path", "")),
            int(args.get("max_chars", DEFAULT_READ_LIMIT)),
        )
    if tool == "write_file":
        return write_file(
            root,
            str(args.get("path", "")),
            str(args.get("content", "")),
            str(args.get("mode", "overwrite")),
        )
    return list_directory(root, str(args.get("path", ".")))


__all__ = [
    "ALLOWED_TOOLS",
    "DEV_TOOLS",
    "FILE_TOOLS",
    "SandboxEscapeError",
    "execute_tool",
    "list_directory",
    "read_file",
    "resolve_in_sandbox",
    "write_file",
]
