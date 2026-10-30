from __future__ import annotations

from typing import Any, TypedDict


class ChunkSource(TypedDict, total=False):
    """片段来源：领域字段（如年报的 company_name/fiscal_year）一律放 extra。"""

    doc_id: str
    title: str
    section: str
    page: int
    uri: str
    extra: dict[str, Any]


class KnowledgeChunk(TypedDict):
    """知识库统一契约（计划 §7.2）：stub 与真实 RAG 服务共用。

    Agent 侧只认这个结构；P3' 从 stub 切到 mcp 时，
    前端渲染、引用编号、审计结构一行都不用改。
    """

    point_id: str
    text: str
    source: ChunkSource
    rrf_score: float
    score_dense: float
    score_sparse: float
    rank_dense: int
    rank_sparse: int
    rerank_score: float
    rerank_rank: int


__all__ = ["ChunkSource", "KnowledgeChunk"]
