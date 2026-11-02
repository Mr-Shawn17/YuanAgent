"""可观测层：Langfuse 追踪与 Prompt 版本化（自托管）。

与现有「执行链路图谱」互补而非替代：
- 执行链路图谱（ExecutionGraph）是给最终用户看的实时可视化，数据落业务表供回放；
- 本模块是给开发者看的全链路账本：标准 trace_id/span、跨进程贯通（含 RAG MCP 服务）、
  Prompt 版本化 + label 发布/回滚、成本/token/延迟聚合。

设计原则：
1. 优雅降级：langfuse 未启用（enabled=False）或 SDK 未安装时，全部 API 变成 no-op，
   绝不阻塞对话主链路，也不改变现有执行图谱/日志行为。
2. 旁路采集：只做观测，不参与业务逻辑，异常一律吞掉只记 warning。
"""

from __future__ import annotations

from app.observability.tracing import (
    get_langfuse,
    is_enabled,
    langfuse_context,
    langfuse_trace_metadata,
    make_callbacks,
    new_trace_id,
)

__all__ = [
    "get_langfuse",
    "is_enabled",
    "langfuse_context",
    "langfuse_trace_metadata",
    "make_callbacks",
    "new_trace_id",
]
