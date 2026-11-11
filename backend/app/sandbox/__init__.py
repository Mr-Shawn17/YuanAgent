"""服务端沙箱模块（spec docs/sandbox-query.md）。

组成：
- validator：git 七件套参数校验器（纯函数，spec §5）；
- repo_source：Mirror 只读副本供给（匿名 clone + TTL fetch，spec §4）；
- container：runsc 容器驱动（docker-py，spec §6-§8）。

注意：docker 库在本模块内懒加载——依赖缺失或 Engine 不可达只应禁用 git_query
（requires_docker 语义），绝不影响其他模块导入。
"""

from app.sandbox.container import (
    SandboxQueryResult,
    SandboxUnavailableError,
    cap_output,
    probe_sandbox_engine,
    run_git_in_sandbox,
    sanitize_output,
)
from app.sandbox.repo_source import RepoSource, RepoSourceError, parse_repo_ref
from app.sandbox.validator import GitArgumentError, validate_git_args

__all__ = [
    "GitArgumentError",
    "RepoSource",
    "RepoSourceError",
    "SandboxQueryResult",
    "SandboxUnavailableError",
    "cap_output",
    "parse_repo_ref",
    "probe_sandbox_engine",
    "run_git_in_sandbox",
    "sanitize_output",
    "validate_git_args",
]
