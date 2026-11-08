"""git_clone / git_commit_push 工具：参数预校验 + local 转发（docs/codex-integration.md）。

命令由用户本机 runner 固定 argv 拼装（runner/gitops.py），凭证经系统凭据库
+ GIT_ASKPASS 注入，PAT 不进模型上下文。本层只做轻量参数校验（服务器侧一道、
runner 侧一道双保险）；无本进程实现，CLI 场景返回可读提示。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from app.tools.base import ToolContext, ToolEntryConfig, tool_error_boundary
from app.tools.local_dispatch import dispatch_local, wants_local

MAX_REPO_URL_CHARS = 500
MAX_COMMIT_MESSAGE_CHARS = 500

_LOCAL_REQUIRED = (
    "[local_runner_required] git 工具需在网页端配合本地执行器（YuanRunner）使用；"
    "终端 CLI 场景请直接在仓库目录执行 git 命令"
)


def _check_repo_url(repo_url: str) -> str:
    """服务器侧轻量校验；完整校验（主机白名单/字符集/内嵌凭证）在 runner 侧二次执行。"""
    text = repo_url.strip()
    if not text:
        return "repo_url 不能为空"
    if len(text) > MAX_REPO_URL_CHARS:
        return f"repo_url 过长（>{MAX_REPO_URL_CHARS} 字符）"
    if not text.lower().startswith("https://"):
        return "仅支持 https:// 的仓库地址（SSH 二期支持）"
    if any(char.isspace() for char in text):
        return "repo_url 不能包含空白字符"
    return ""


def make_git_clone_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    use_local = wants_local(entry, context)

    @tool("git_clone")
    @tool_error_boundary
    def git_clone(repo_url: str, target_dir: str = "", branch: str = "") -> str:
        """克隆一个 git 仓库到本地工作目录；同名仓库已存在时改为 git pull 更新（幂等）。
        属于危险操作，执行前需要用户确认。

        Args:
            repo_url: 仓库地址（仅 https，主机需在执行器白名单 runner.yaml git.allowed_hosts 内）。
            target_dir: 工作目录下的目标子目录名；默认取仓库名。
            branch: 要拉取的分支；默认为仓库默认分支。
        """
        problem = _check_repo_url(repo_url)
        if problem:
            return f"[参数无效] {problem}"
        if not use_local:
            return _LOCAL_REQUIRED
        return dispatch_local(
            context,
            tool_name="git_clone",
            args={
                "repo_url": repo_url.strip(),
                "target_dir": target_dir.strip(),
                "branch": branch.strip(),
            },
            timeout=context.settings.local_devtools_dispatch_timeout,
        )

    return git_clone


def make_git_commit_push_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    use_local = wants_local(entry, context)

    @tool("git_commit_push")
    @tool_error_boundary
    def git_commit_push(message: str) -> str:
        """把本地工作目录内已克隆仓库的全部改动提交（git add -A + commit）并推回原远程仓库。
        只推回 git_clone 时登记的远程与分支，不能改推送目标。属于危险操作，执行前需要用户确认。

        Args:
            message: commit 说明，建议一句话讲清改动（不超过 500 字符）。
        """
        text = message.strip()
        if not text:
            return "[参数无效] message 不能为空"
        if len(text) > MAX_COMMIT_MESSAGE_CHARS:
            return f"[参数无效] message 过长（>{MAX_COMMIT_MESSAGE_CHARS} 字符），请精简说明"
        if not use_local:
            return _LOCAL_REQUIRED
        return dispatch_local(
            context,
            tool_name="git_commit_push",
            args={"message": text},
            timeout=context.settings.local_devtools_dispatch_timeout,
        )

    return git_commit_push


__all__ = ["make_git_clone_tool", "make_git_commit_push_tool"]
