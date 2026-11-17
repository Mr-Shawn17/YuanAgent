"""git_query / rg_query 工具：服务端 runsc 沙箱内的只读查询（spec docs/sandbox-query.md
+ docs/opensandbox-migration.md v1.1）。

与 gitops 四件套（走本机 runner）完全不同的执行面：命令在服务端 gVisor 沙箱内对
只读副本（Mirror）执行，写路径不经此工具。硬不变量「无沙箱不执行」（spec §2）：
沙箱不可用报 sandbox_unavailable 拒绝执行，绝不回退宿主直跑；装配期探活失败
（server 不可达 / secure_runtime 非 gvisor）则工具整体自禁（requires_opensandbox
语义，同 requires_key 先例，spec §10.3）。

执行形态（opensandbox 驱动，默认）：会话级沙箱池复用同一沙箱（1 会话 1 沙箱），
工具以 content_and_artifact 返回——artifact 承载沙箱审计补充字段（sandbox_id/
镜像/退出码/truncated，TC-16），中间件落审计时并入 args_redacted。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.core.logging import get_logger
from app.sandbox import (
    GitArgumentError,
    RepoSource,
    RepoSourceError,
    SandboxQueryResult,
    SandboxUnavailableError,
    probe_sandbox_engine,
    run_git_in_sandbox,
    run_rg_in_sandbox,
    validate_git_args,
    validate_rg_args,
    validate_rg_path,
)
from app.tools.base import ToolContext, ToolEntryConfig, tool_error_boundary

logger = get_logger(__name__)

MAX_RG_PATTERN_CHARS = 256  # 与校验器选项值上限一致


def _sandbox_artifact(result: SandboxQueryResult, driver: str) -> dict[str, Any]:
    """沙箱审计补充字段（spec §9/TC-16）：随 ToolMessage.artifact 走，仅进审计不进模型。"""
    # yuan_audit 信封：langchain 会自动给 ToolMessage 附 structured_content artifact，
    # 中间件只认本信封键，避免把其他 artifact 误并入审计
    return {
        "yuan_audit": {
            "sandbox_driver": driver,
            "sandbox_id": result.sandbox_id,
            "sandbox_image": "yuan-sandbox-exec" if driver == "opensandbox" else "yuan-sandbox-git",
            "exit_code": result.exit_code,
            "truncated": result.truncated,
            "mirror_ref": result.mirror_ref,
        }
    }


def make_git_query_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool | None:
    """装配工厂：探活失败返回 None（registry 记禁用原因，requires_opensandbox 语义）。"""
    settings = context.settings
    unavailable_reason = probe_sandbox_engine(settings)
    if unavailable_reason is not None:
        logger.warning("git_query_disabled", reason=unavailable_reason)
        return None
    source = RepoSource(settings)
    session_id = context.session_id

    @tool("git_query", response_format="content_and_artifact")
    @tool_error_boundary(content_and_artifact=True)
    async def git_query(
        repo_ref: str, subcommand: str, argv: list[str] | None = None
    ) -> tuple[str, dict[str, Any]]:
        """查询公开 git 仓库的提交历史/某次提交内容/版本差异/逐行归属。
        输入仓库 owner/repo 与 git 查询意图，返回命令输出。

        仅支持 GitHub 公开仓库；命令在服务端 gVisor 沙箱内只读执行，无网络。
        subcommand 限七件套：log（历史）、show（某次提交）、diff（版本对比）、
        blame（逐行归属）、branch（分支）、tag（标签）、rev-parse（ref 解析）；
        argv 为该子命令的选项与位置参数（每子命令选项白名单，越界会被拒绝）。

        Args:
            repo_ref: GitHub "owner/repo"，如 "Mr-Shawn17/YuanAgent"。
            subcommand: git 子命令，七件套之一。
            argv: 子命令参数列表，如 ["--oneline", "--max-count=10"]；可省略。
        """
        try:
            validated_args = validate_git_args(subcommand, list(argv or []))
        except GitArgumentError as exc:
            return f"[参数无效] {exc}", {}

        try:
            await asyncio.to_thread(source.resolve, repo_ref)
        except RepoSourceError as exc:
            return str(exc), {}
        engine_mount_src = source.engine_mount_src(repo_ref)

        try:
            result = await run_git_in_sandbox(
                settings, session_id, engine_mount_src, repo_ref, subcommand, validated_args
            )
        except SandboxUnavailableError as exc:
            # 硬不变量（spec §2）：沙箱不可用直接报错返回，绝不回退宿主直跑
            return str(exc), {}

        status = "exit 0" if result.ok else f"exit {result.exit_code}"
        header = (
            f"[git {result.subcommand} {status}"
            f" · {result.elapsed_ms}ms · mirror {result.mirror_ref}]"
        )
        content = f"{header}\n{result.stdout}" if result.stdout.strip() else header
        return content, _sandbox_artifact(result, settings.sandbox_query.driver)

    return git_query


def make_rg_query_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool | None:
    """rg_query 装配工厂：与 git_query 同一探活与镜像来源（requires_opensandbox 语义）。"""
    settings = context.settings
    unavailable_reason = probe_sandbox_engine(settings)
    if unavailable_reason is not None:
        logger.warning("rg_query_disabled", reason=unavailable_reason)
        return None
    source = RepoSource(settings)
    session_id = context.session_id

    @tool("rg_query", response_format="content_and_artifact")
    @tool_error_boundary(content_and_artifact=True)
    async def rg_query(
        repo_ref: str, pattern: str, path: str = "", argv: list[str] | None = None
    ) -> tuple[str, dict[str, Any]]:
        """在公开 git 仓库的代码内容里做正则检索（ripgrep，只读）。
        输入仓库 owner/repo、检索模式与可选路径限定，返回命中行。

        与 git_query 共用服务端 gVisor 沙箱与只读副本，无网络。
        默认遵守 .gitignore（跳过 .git 等隐藏目录）；输出封顶 20000 字符。

        Args:
            repo_ref: GitHub "owner/repo"，如 "Mr-Shawn17/YuanAgent"。
            pattern: 检索模式（rust 正则），如 "sandbox_query"、"def resolve"。
            path: 可选，限定目录/文件（仓库内相对路径，正斜杠），如 "backend/app"。
            argv: 额外白名单选项：-i 忽略大小写、-w 全词、-n 行号、-g <glob>、
                  -t <类型>、-m <每文件上限>、-A/-B/-C <上下文行数>；可省略。
        """
        text = pattern.strip()
        if not text:
            return "[参数无效] pattern 不能为空", {}
        if len(text) > MAX_RG_PATTERN_CHARS:
            return f"[参数无效] pattern 过长（>{MAX_RG_PATTERN_CHARS} 字符）", {}
        try:
            validated_argv = validate_rg_args(list(argv or []))
            normalized_path = validate_rg_path(path)
        except GitArgumentError as exc:
            return f"[参数无效] {exc}", {}

        try:
            await asyncio.to_thread(source.resolve, repo_ref)
        except RepoSourceError as exc:
            return str(exc), {}
        engine_mount_src = source.engine_mount_src(repo_ref)

        try:
            result = await run_rg_in_sandbox(
                settings,
                session_id,
                engine_mount_src,
                repo_ref,
                text,
                normalized_path,
                validated_argv,
            )
        except SandboxUnavailableError as exc:
            return str(exc), {}  # 硬不变量（spec §2）：绝不回退宿主直跑

        header = (
            f"[rg {result.exit_code} · {result.elapsed_ms}ms · mirror {result.mirror_ref}]"
        )
        if result.exit_code == 1 and not result.stdout.strip():
            # rg 约定：exit 1 = 无命中（合法结果，非错误）
            return f"{header} 无匹配", _sandbox_artifact(result, settings.sandbox_query.driver)
        content = f"{header}\n{result.stdout}" if result.stdout.strip() else header
        return content, _sandbox_artifact(result, settings.sandbox_query.driver)

    return rg_query


__all__ = ["make_git_query_tool", "make_rg_query_tool"]
