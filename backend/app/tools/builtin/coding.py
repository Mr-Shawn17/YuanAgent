"""delegate_coding 工具：把编程任务整段委派给用户本机的 codex CLI（danger 档）。

token 经济分层（docs/codex-integration.md §1）：编程类重任务走用户本机
codex 自己的账号，服务端 key 只负责轻量编排。codex 以 workspace-write
模式运行（其沙箱为附加保险，平台安全支柱是结构化收口 + 审批 + 审计）。
执行在 runner 侧（runner/coding.py）；本层仅参数校验 + local 转发。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from app.tools.base import ToolContext, ToolEntryConfig, tool_error_boundary
from app.tools.local_dispatch import dispatch_local, wants_local

MAX_TASK_CHARS = 8000

_LOCAL_REQUIRED = (
    "[local_runner_required] codex 委派需在网页端配合本地执行器（YuanRunner）使用；"
    "终端 CLI 场景可直接运行 codex exec"
)


def make_delegate_coding_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    use_local = wants_local(entry, context)

    @tool("delegate_coding")
    @tool_error_boundary
    def delegate_coding(task: str) -> str:
        """把一段编程任务整段委派给本机的 codex CLI 执行（用它自己的沙箱与账号）。
        适合多文件修改、跑测试修错等重编程任务；轻量单文件修改用 write_file 更省。
        属于危险操作，执行前需要用户确认。

        Args:
            task: 编程任务的自然语言描述（目标、涉及文件、验收标准），越具体越好，不超过 8000 字符。
        """
        text = (task or "").strip()
        if not text:
            return "[参数无效] task 不能为空"
        if len(text) > MAX_TASK_CHARS:
            return f"[参数无效] task 过长（>{MAX_TASK_CHARS} 字符），请拆分任务"
        if not use_local:
            return _LOCAL_REQUIRED
        return dispatch_local(
            context,
            tool_name="delegate_coding",
            args={"task": text},
            # codex 是长任务：用独立的长超时（Settings.local_codex_dispatch_timeout）
            timeout=context.settings.local_codex_dispatch_timeout,
        )

    return delegate_coding


__all__ = ["MAX_TASK_CHARS", "make_delegate_coding_tool"]
