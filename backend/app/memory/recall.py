"""记忆召回：结构化字段过滤 + Python 侧关键词匹配。

MySQL store 的 search 只支持 prefix LIKE 与 JSON 字段过滤、无语义向量检索（计划风险 R3），
所以召回策略是：
1. 按 ("memories", user_id) 前缀一次拉回候选（updated_at 倒序）；
2. profile/preference 属常驻记忆，无条件保留（各自只取最新若干条）；
3. event 类按「关键词/内容与本轮用户输入的匹配得分」筛选，0 分不注入；
4. 得分相同时新记忆优先，总量截断到 limit。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from langgraph.store.base import BaseStore

from app.memory.schema import (
    ALWAYS_RECALL_KINDS,
    KIND_LABELS,
    MemoryRecord,
    kind_from_namespace,
    user_namespace,
)

# 候选拉取上限：单用户记忆条数在这个量级内全量扫描成本可忽略
CANDIDATE_LIMIT = 200
DEFAULT_RECALL_LIMIT = 8
# 常驻类记忆每 kind 最多注入条数，防止偏好越攒越多撑爆提示词
ALWAYS_PER_KIND_LIMIT = 4
# 单条内容注入提示词时的截断长度
CONTENT_PREVIEW_LIMIT = 160

_WORD_PATTERN = re.compile(r"[a-z0-9]+")
_CJK_RUN_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,}")


@dataclass(slots=True)
class RecalledMemory:
    """注入提示词用的一条召回结果。"""

    kind: str
    content: str
    keywords: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    score: int = 0

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def preview(self) -> str:
        text = " ".join(self.content.split())
        if len(text) <= CONTENT_PREVIEW_LIMIT:
            return text
        return text[:CONTENT_PREVIEW_LIMIT] + "…"


def tokenize(text: str) -> set[str]:
    """轻量分词：英文/数字按词切，中文连续段切成 2-gram。

    不引入分词库（项目无 jieba 依赖），2-gram 对关键词级匹配已经够用。
    """
    lowered = text.lower()
    tokens = set(_WORD_PATTERN.findall(lowered))
    for run in _CJK_RUN_PATTERN.findall(lowered):
        if len(run) == 2:
            tokens.add(run)
            continue
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def score_record(query: str, query_tokens: set[str], record: MemoryRecord) -> int:
    """一条记忆与本轮用户输入的相关性得分。

    - keywords 整词出现在原文里：+3/个（最强信号，写入时就是为召回准备的）；
    - keywords 分词后与查询分词有交集：+2/个；
    - 查询 token（长度≥2）出现在 content 里：+1/个。
    """
    score = 0
    content = str(record.get("content", ""))
    content_lower = content.lower()
    for keyword in record.get("keywords", []) or []:
        keyword_lower = str(keyword).lower().strip()
        if not keyword_lower:
            continue
        if keyword_lower in query.lower():
            score += 3
            continue
        if tokenize(keyword_lower) & query_tokens:
            score += 2
    for token in query_tokens:
        if len(token) >= 2 and token in content_lower:
            score += 1
    return score


def _to_recalled(
    namespace: tuple[str, ...], value: object, *, score: int = 0
) -> RecalledMemory | None:
    if not isinstance(value, dict):
        return None
    content = str(value.get("content", "")).strip()
    if not content:
        return None
    kind = kind_from_namespace(namespace)
    record_kind = value.get("kind")
    if isinstance(record_kind, str) and record_kind:
        kind = record_kind
    keywords_raw = value.get("keywords") or []
    keywords = [str(item) for item in keywords_raw] if isinstance(keywords_raw, list) else []
    return RecalledMemory(
        kind=kind,
        content=content,
        keywords=keywords,
        created_at=str(value.get("created_at", "")),
        updated_at=str(value.get("updated_at", "")),
        score=score,
    )


async def recall_memories(
    store: BaseStore,
    user_id: str,
    query: str,
    *,
    limit: int = DEFAULT_RECALL_LIMIT,
    candidate_limit: int = CANDIDATE_LIMIT,
) -> list[RecalledMemory]:
    """按用户前缀拉候选，常驻类直取、event 类按得分筛，返回待注入的记忆列表。"""
    items = await store.asearch(user_namespace(user_id), limit=candidate_limit)

    always: list[RecalledMemory] = []
    scored: list[RecalledMemory] = []
    query_tokens = tokenize(query) if query.strip() else set[str]()

    for item in items:
        recalled = _to_recalled(item.namespace, item.value or {})
        if recalled is None:
            continue
        if recalled.kind in ALWAYS_RECALL_KINDS:
            recalled.score = score_record(query, query_tokens, _as_record(item.value))
            always.append(recalled)
            continue
        score = score_record(query, query_tokens, _as_record(item.value))
        if score > 0:
            recalled.score = score
            scored.append(recalled)

    # asearch 已按 updated_at 倒序，这里做稳定截断：每 kind 保留最新 N 条
    per_kind: dict[str, int] = {}
    kept_always: list[RecalledMemory] = []
    for recalled in always:
        count = per_kind.get(recalled.kind, 0)
        if count >= ALWAYS_PER_KIND_LIMIT:
            continue
        per_kind[recalled.kind] = count + 1
        kept_always.append(recalled)

    # 稳定排序两步走：先按更新时间倒序，再按得分倒序（同分新记忆在前）
    scored.sort(key=lambda item: item.updated_at, reverse=True)
    scored.sort(key=lambda item: item.score, reverse=True)

    combined = kept_always + scored
    return combined[: max(1, limit)]


def _as_record(value: object) -> MemoryRecord:
    """store 里的 value 是 JSON dict，宽松转成 MemoryRecord 供打分。"""
    if not isinstance(value, dict):
        return MemoryRecord(content="")
    record = MemoryRecord(content=str(value.get("content", "")))
    keywords = value.get("keywords")
    if isinstance(keywords, list):
        record["keywords"] = [str(item) for item in keywords]
    return record


__all__ = [
    "ALWAYS_PER_KIND_LIMIT",
    "CANDIDATE_LIMIT",
    "CONTENT_PREVIEW_LIMIT",
    "DEFAULT_RECALL_LIMIT",
    "RecalledMemory",
    "recall_memories",
    "score_record",
    "tokenize",
]
