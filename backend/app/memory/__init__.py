"""长期记忆子系统：域模型（schema）、召回（recall）、抽取合并（extractor）。

对应计划 §P4'：命名空间 ("memories", user_id, kind)，kind ∈ {profile, preference, event}；
召回走「结构化过滤 + Python 侧关键词匹配」，抽取合并走 langmem。
"""

from app.memory.extractor import MemoryExtractor, MemoryManager
from app.memory.recall import RecalledMemory, recall_memories, score_record, tokenize
from app.memory.schema import (
    ALWAYS_RECALL_KINDS,
    DEFAULT_KIND,
    KIND_LABELS,
    MEMORY_KINDS,
    MEMORY_NAMESPACE,
    MemoryRecord,
    build_record,
    kind_from_namespace,
    memory_namespace,
    normalize_kind,
    user_namespace,
)

__all__ = [
    "ALWAYS_RECALL_KINDS",
    "DEFAULT_KIND",
    "KIND_LABELS",
    "MEMORY_KINDS",
    "MEMORY_NAMESPACE",
    "MemoryExtractor",
    "MemoryManager",
    "MemoryRecord",
    "RecalledMemory",
    "build_record",
    "kind_from_namespace",
    "memory_namespace",
    "normalize_kind",
    "recall_memories",
    "score_record",
    "tokenize",
    "user_namespace",
]
