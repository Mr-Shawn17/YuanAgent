"""delegate_coding 工具：把编程任务整段委派给用户本机的 codex CLI（danger 档）。

token 经济分层（见 docs/roadmap.md）：编程类重任务走用户本机 codex 自己的账号，
服务端 key 只负责轻量编排。codex 自带沙箱管它内部循环的命令。

- Web 模式（mode: local）：经 LocalTaskHub 转发，runner 侧执行（runner/coding.py）
- CLI 模式：本进程直接 spawn codex（与 runner/coding.py 同规则，改动双向同步）
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import uuid
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from app.tools.base import ToolContext, ToolEntryConfig, tool_error_boundary
from app.tools.builtin.shell import build_sandbox_env, kill_process_tree

CODEX_TIMEOUT_SECONDS = 600
CODEX_MAX_TASK_CHARS = 8000

_NOT_INSTALLED_TEXT = (
    "[codex_not_installed] 未检测到 codex CLI。安装并登录后重试：\n"
    "  npm install -g @openai/codex\n"
    "  codex login"
)


def _wants_local(entry: ToolEntryConfig, context: ToolContext) -> bool:
    return entry.mode.strip().lower() == "local" and context.local_router is not None


def _detect_codex() -> str | None:
    return shutil.which("codex")


def _run_codex_local(task: str, root: Path) -> str:
    """CLI 模式：本进程 spawn codex（与 runner/coding.py 同规则）。"""
    codex = _detect_codex()
    if codex is None:
        return _NOT_INSTALLED_TEXT
    out_dir = root / ".yuan" / "codex"
    out_dir.mkdir(parents=True, exist_ok=True)
    last_message = out_dir / f"last_{uuid.uuid4().hex[:8]}.md"
    argv = [
        codex, "exec",
        "--json", "--ephemeral", "--skip-git-repo-check",
        "-s", "workspace-write",
        "-C", str(root),
        "-o", str(last_message),
        task,
    ]
    try:
        proc = subprocess.Popen(
            argv, cwd=str(root), env=build_sandbox_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        return f"[工具执行失败] 无法启动 codex：{exc}"
    try:
        out, err = proc.communicate(timeout=CODEX_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        with contextlib.suppress(Exception):
            proc.kill()
        proc.communicate()
        return f"[执行超时] codex 任务超过 {CODEX_TIMEOUT_SECONDS} 秒被终止（进程树已结束）"
    message = ""
    if last_message.is_file():
        message = last_message.read_text(encoding="utf-8", errors="replace").strip()
        with contextlib.suppress(OSError):
            last_message.unlink(missing_ok=True)
    if not message:
        message = (out or b"").decode("utf-8", errors="replace")[-2000:].strip()
    if proc.returncode != 0:
        err_tail = (err or b"").decode("utf-8", errors="replace")[-500:].strip()
        hint = f"\n[stderr 尾部]\n{err_tail}" if err_tail else ""
        return f"[codex 退出码 {proc.returncode}]\n{message[:20000]}{hint}"
    return message[:20000] if message else "（codex 无输出，exit 0）"


def make_delegate_coding_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    root = context.sandbox()
    use_local = _wants_local(entry, context)

    @tool("delegate_coding")
    @tool_error_boundary
    def delegate_coding(task: str) -> str:
        """把一段编程任务整段委派给本机的 codex CLI（用它自己的沙箱与账号执行）。
        适合多文件修改、跑测试修错等重编程任务；轻量单文件修改用 write_file 更省。
        属于危险操作，执行前需要用户确认。

        Args:
            task: 编程任务的自然语言描述（目标、涉及文件、验收标准），越具体越好。
        """
        text = (task or "").strip()
        if not text:
            return "[工具执行失败] task 不能为空"
        if len(text) > CODEX_MAX_TASK_CHARS:
            return f"[工具执行失败] task 过长（>{CODEX_MAX_TASK_CHARS} 字符），请拆分任务"
        if use_local:
            from app.tools.builtin.shell import dispatch_local

            return dispatch_local(
                context,
                tool_name="delegate_coding",
                args={"task": text},
                timeout=CODEX_TIMEOUT_SECONDS + 120.0,
            )
        return _run_codex_local(text, root)

    return delegate_coding


__all__ = ["make_delegate_coding_tool"]
