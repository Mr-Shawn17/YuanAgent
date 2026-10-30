"""长期记忆的域模型：命名空间、记录结构与 langmem 抽取 schema。

对应计划 §P4'：命名空间 ("memories", user_id, kind)，kind ∈ {profile, preference, event}。
MySQL store 的 search 只做 prefix LIKE + JSON 字段过滤（无语义检索，见风险 R3），
因此记录里冗余 keywords 字段，供召回侧做关键词匹配。
"""

from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel, Field

from app.db import utcnow

MEMORY_NAMESPACE = "memories"
ANONYMOUS_USER = "anonymous"

KIND_PROFILE = "profile"
KIND_PREFERENCE = "preference"
KIND_EVENT = "event"
MEMORY_KINDS: frozenset[str] = frozenset({KIND_PROFILE, KIND_PREFERENCE, KIND_EVENT})
DEFAULT_KIND = KIND_EVENT

KIND_LABELS: dict[str, str] = {
    KIND_PROFILE: "用户档案",
    KIND_PREFERENCE: "偏好习惯",
    KIND_EVENT: "事件经历",
}

# profile/preference 属于"常驻记忆"：每轮无条件注入，保证跨会话记得你是谁、喜欢什么；
# event 数量会持续增长，只按关键词相关性召回。
ALWAYS_RECALL_KINDS: tuple[str, ...] = (KIND_PROFILE, KIND_PREFERENCE)

SOURCE_TOOL = "tool"
SOURCE_EXTRACT = "extract"


class MemoryRecord(TypedDict, total=False):
    """store 中一条记忆的 value 结构（JSON 列，字段均可被 filter 查询）。"""

    content: str
    kind: str
    keywords: list[str]
    created_at: str
    updated_at: str
    session_id: str
    source: str


def memory_namespace(user_id: str, kind: str) -> tuple[str, str, str]:
    """三段式命名空间：("memories", user_id, kind)。"""
    user = user_id.strip() or ANONYMOUS_USER
    resolved = kind if kind in MEMORY_KINDS else DEFAULT_KIND
    return (MEMORY_NAMESPACE, user, resolved)


def user_namespace(user_id: str) -> tuple[str, str]:
    """用户级前缀（召回时一次拉全 kind 的候选）。"""
    return (MEMORY_NAMESPACE, user_id.strip() or ANONYMOUS_USER)


def kind_from_namespace(namespace: tuple[str, ...]) -> str:
    """从命名空间反解 kind，非法段回退 event。"""
    kind = namespace[2] if len(namespace) >= 3 else ""
    return kind if kind in MEMORY_KINDS else DEFAULT_KIND


def build_record(
    *,
    kind: str,
    content: str,
    keywords: list[str] | None = None,
    session_id: str = "",
    source: str = SOURCE_TOOL,
    created_at: str | None = None,
) -> MemoryRecord:
    """构造一条记忆记录；created_at 传入时保留（更新场景），否则取当前时间。"""
    now = utcnow().isoformat(timespec="seconds")
    return MemoryRecord(
        content=content,
        kind=kind,
        keywords=[str(item).strip() for item in (keywords or []) if str(item).strip()],
        created_at=created_at or now,
        updated_at=now,
        session_id=session_id,
        source=source,
    )


class ProfileMemory(BaseModel):
    """用户的稳定身份信息：姓名/称呼、职业、所在城市、长期目标等不常变化的事实。"""

    content: str = Field(description="一条独立成句、脱离上下文也能看懂的用户身份信息")
    keywords: list[str] = Field(
        default_factory=list, description="3~5 个便于日后检索的中文或英文关键词"
    )


class PreferenceMemory(BaseModel):
    """用户的偏好与习惯：口味、兴趣、沟通习惯、明确表达过的喜欢/讨厌。"""

    content: str = Field(description="一条独立成句、脱离上下文也能看懂的偏好描述")
    keywords: list[str] = Field(
        default_factory=list, description="3~5 个便于日后检索的中文或英文关键词"
    )


class EventMemory(BaseModel):
    """发生过的具体事件：某天的活动、一次性约定、短期计划、值得记住的对话结论。"""

    content: str = Field(description="一条独立成句、脱离上下文也能看懂的事件描述，尽量带上时间")
    keywords: list[str] = Field(
        default_factory=list, description="3~5 个便于日后检索的中文或英文关键词"
    )


EXTRACT_SCHEMAS: tuple[type[BaseModel], ...] = (
    ProfileMemory,
    PreferenceMemory,
    EventMemory,
)

SCHEMA_KINDS: dict[type[BaseModel], str] = {
    ProfileMemory: KIND_PROFILE,
    PreferenceMemory: KIND_PREFERENCE,
    EventMemory: KIND_EVENT,
}

KIND_SCHEMAS: dict[str, type[BaseModel]] = {kind: schema for schema, kind in SCHEMA_KINDS.items()}


def normalize_kind(value: str) -> str:
    """把外部传入的 kind 归一到合法值，非法时回退 event。"""
    kind = value.strip().lower()
    return kind if kind in MEMORY_KINDS else DEFAULT_KIND


__all__ = [
    "ALWAYS_RECALL_KINDS",
    "ANONYMOUS_USER",
    "DEFAULT_KIND",
    "EXTRACT_SCHEMAS",
    "KIND_EVENT",
    "KIND_LABELS",
    "KIND_PREFERENCE",
    "KIND_PROFILE",
    "KIND_SCHEMAS",
    "MEMORY_KINDS",
    "MEMORY_NAMESPACE",
    "SCHEMA_KINDS",
    "SOURCE_EXTRACT",
    "SOURCE_TOOL",
    "EventMemory",
    "MemoryRecord",
    "PreferenceMemory",
    "ProfileMemory",
    "build_record",
    "kind_from_namespace",
    "memory_namespace",
    "normalize_kind",
    "user_namespace",
]
