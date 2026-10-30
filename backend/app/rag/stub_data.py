from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import PROJECT_ROOT
from app.core.errors import ConfigError
from app.rag.schema import ChunkSource, KnowledgeChunk

# stub 语料位置：P3' 切换 mode: mcp 后此文件不再被读取，但保留作离线演示/测试
KB_STUB_PATH = Path("data/kb_stub/chunks.json")
RRF_K = 60  # RRF 平滑常数，与混合检索RAG 项目保持一致
MAX_TOP_K = 20
# 打分权重：覆盖率为主、词频为辅（词频单项封顶，防止长文档刷分）
COVERAGE_WEIGHT = 0.7
FREQ_WEIGHT = 0.3
FREQ_CAP = 5

_ASCII_TOKEN = re.compile(r"[a-z0-9]+")
_COMPACT_KEEP = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")


def _features(text: str) -> Counter[str]:
    """把文本压成可比较的特征袋：ASCII 词 + 字符 bigram（对中文即「字对」）。"""
    lowered = text.lower()
    tokens = _ASCII_TOKEN.findall(lowered)
    compact = _COMPACT_KEEP.sub("", lowered)
    grams = [compact[i : i + 2] for i in range(len(compact) - 1)]
    return Counter(tokens + grams)


def score_text(query: str, corpus: str) -> float:
    """轻量相关性打分（0~1）：查询特征在语料中的覆盖率 + 截断词频。"""
    query_features = _features(query)
    if not query_features:
        return 0.0
    corpus_features = _features(corpus)
    if not corpus_features:
        return 0.0
    total = len(query_features)
    coverage = sum(1 for key in query_features if corpus_features.get(key, 0) > 0) / total
    freq = sum(min(corpus_features.get(key, 0), FREQ_CAP) for key in query_features) / (
        FREQ_CAP * total
    )
    return round(COVERAGE_WEIGHT * coverage + FREQ_WEIGHT * freq, 6)


def _normalize_source(raw: Any) -> ChunkSource:
    """把 JSON 里的 source 补齐成契约字段（缺失项填 "" / 0 / {}）。"""
    data = raw if isinstance(raw, dict) else {}
    extra = data.get("extra")
    page = data.get("page", 0)
    return ChunkSource(
        doc_id=str(data.get("doc_id", "")),
        title=str(data.get("title", "")),
        section=str(data.get("section", "")),
        page=int(page) if isinstance(page, int) else 0,
        uri=str(data.get("uri", "")),
        extra=dict(extra) if isinstance(extra, dict) else {},
    )


def make_chunk(
    *, point_id: str, text: str, source: ChunkSource, score: float, rank: int
) -> KnowledgeChunk:
    """按 §7.2 契约填充全部字段：stub 只有一路打分，dense/sparse/rerank 同值同排名。"""
    return KnowledgeChunk(
        point_id=point_id,
        text=text,
        source=source,
        rrf_score=round(2.0 / (RRF_K + rank), 6),
        score_dense=score,
        score_sparse=score,
        rank_dense=rank,
        rank_sparse=rank,
        rerank_score=score,
        rerank_rank=rank,
    )


@dataclass
class StubKnowledgeBase:
    """data/kb_stub/chunks.json 的内存检索器：懒加载 + 缓存，无第三方依赖。"""

    path: Path | None = None
    _chunks: list[tuple[str, str, ChunkSource]] = field(default_factory=list, repr=False)
    _loaded: bool = field(default=False, repr=False)

    def resolve_path(self) -> Path:
        raw = self.path or PROJECT_ROOT / KB_STUB_PATH
        return raw if raw.is_absolute() else PROJECT_ROOT / raw

    def load(self) -> None:
        if self._loaded:
            return
        file_path = self.resolve_path()
        if not file_path.is_file():
            msg = f"stub 知识库语料缺失：{file_path}"
            raise ConfigError(msg)
        try:
            entries = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            msg = f"stub 知识库语料不是合法 JSON：{file_path}（{exc}）"
            raise ConfigError(msg) from exc
        if not isinstance(entries, list):
            msg = f"stub 知识库语料顶层必须是数组：{file_path}"
            raise ConfigError(msg)
        chunks: list[tuple[str, str, ChunkSource]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text", "")).strip()
            point_id = str(entry.get("point_id", "")).strip()
            if not text or not point_id:
                continue
            chunks.append((point_id, text, _normalize_source(entry.get("source"))))
        if not chunks:
            msg = f"stub 知识库语料为空：{file_path}"
            raise ConfigError(msg)
        self._chunks = chunks
        self._loaded = True

    def count(self) -> int:
        self.load()
        return len(self._chunks)

    def search(self, query: str, *, top_k: int = 5) -> list[KnowledgeChunk]:
        """按打分降序返回 top_k 个片段；0 分（完全不相关）的片段不返回。"""
        self.load()
        limit = max(1, min(top_k, MAX_TOP_K))
        scored: list[tuple[float, int, str, str, ChunkSource]] = []
        for index, (point_id, text, source) in enumerate(self._chunks):
            title = source.get("title", "")
            score = score_text(query, f"{text} {title}")
            if score > 0:
                scored.append((score, index, point_id, text, source))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            make_chunk(point_id=point_id, text=text, source=source, score=score, rank=rank)
            for rank, (score, _index, point_id, text, source) in enumerate(scored[:limit], start=1)
        ]


def search_stub(query: str, *, top_k: int = 5, path: Path | None = None) -> list[KnowledgeChunk]:
    """模块级便捷入口（测试/脚本用），每次新建实例避免共享缓存。"""
    return StubKnowledgeBase(path=path).search(query, top_k=top_k)


def chunk_count(path: Path | None = None) -> int:
    return StubKnowledgeBase(path=path).count()


__all__ = [
    "FREQ_CAP",
    "KB_STUB_PATH",
    "MAX_TOP_K",
    "RRF_K",
    "StubKnowledgeBase",
    "chunk_count",
    "make_chunk",
    "score_text",
    "search_stub",
]
