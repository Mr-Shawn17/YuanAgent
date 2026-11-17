"""服务端沙箱模块（spec docs/sandbox-query.md + docs/opensandbox-migration.md v1.1）。

组成：
- validator：git 七件套参数校验器（纯函数，spec sandbox-query §5）；
- repo_source：Mirror 只读副本供给（匿名 clone + TTL fetch，spec §4）；
- container：沙箱驱动（opensandbox 会话级沙箱池为默认，docker-py 直连为回滚开关，
  spec v1.1 §11）；
- pool：会话级沙箱池（session→sandbox 映射 + 空闲 TTL 回收 + 会话销毁，spec §4）。

注意：docker 库与 opensandbox SDK 均在模块内懒加载——依赖缺失或 server/Engine 不可达
只应禁用沙箱查询（requires_docker / requires_opensandbox 语义），绝不影响其他模块导入。
"""

from app.sandbox.container import (
    SandboxQueryResult,
    SandboxUnavailableError,
    cap_output,
    probe_sandbox_engine,
    run_git_in_sandbox,
    run_rg_in_sandbox,
    sanitize_output,
)
from app.sandbox.pool import SandboxPool, get_pool, reset_pool
from app.sandbox.repo_source import RepoSource, RepoSourceError, parse_repo_ref
from app.sandbox.validator import (
    GitArgumentError,
    validate_git_args,
    validate_rg_args,
    validate_rg_path,
)

__all__ = [
    "GitArgumentError",
    "RepoSource",
    "RepoSourceError",
    "SandboxPool",
    "SandboxQueryResult",
    "SandboxUnavailableError",
    "cap_output",
    "get_pool",
    "parse_repo_ref",
    "probe_sandbox_engine",
    "reset_pool",
    "run_git_in_sandbox",
    "run_rg_in_sandbox",
    "sanitize_output",
    "validate_git_args",
    "validate_rg_args",
    "validate_rg_path",
]
