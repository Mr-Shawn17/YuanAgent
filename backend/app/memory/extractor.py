"""langmem 记忆抽取/合并：从对话消息里提取结构化记忆并增量写回 store。

对应计划 §P4'「langmem 用于抽取/合并」：
- 抽取：create_memory_manager + 三个自定义 schema（profile/preference/event），
  fast 模型跑后台反射，不阻塞对话主链路；
- 合并：把 store 里既有记忆以 (id, schema 实例) 传给 manager（enable_updates=True），
  同 id 返回即更新原条目（保留 created_at），新 id 才插入，避免重复堆积。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ValidationError

from app.core.logging import get_logger
from app.db import uuid7
from app.memory.schema import (
    EXTRACT_SCHEMAS,
    KIND_SCHEMAS,
    SCHEMA_KINDS,
    SOURCE_EXTRACT,
    MemoryRecord,
    build_record,
    kind_from_namespace,
    memory_namespace,
    user_namespace,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langgraph.store.base import BaseStore

logger = get_logger(__name__)

# 抽取时喂给 manager 的消息窗口上限（只取最近一段，控制 fast 模型成本）
EXTRACT_MESSAGE_WINDOW = 40
# 合并基线：拉回既有记忆的条数上限
EXISTING_LIMIT = 200

EXTRACT_INSTRUCTIONS = """从对话中抽取值得长期记住的用户信息，按三类 schema 归档：
- ProfileMemory：稳定身份信息（称呼、职业、城市、长期目标）；
- PreferenceMemory：偏好与习惯（口味、兴趣、沟通偏好、明确的喜欢/讨厌）；
- EventMemory：具体事件与短期约定（尽量带上时间）。

要求：
1. 只记对未来的对话有帮助的信息，寒暄、一次性问答、你自己的回复内容不要记；
2. 每条记忆独立成句，脱离本次对话也能看懂，用简体中文；
3. 与 existing 中已有记忆语义重复的不要再新增；
   已有记忆过时或被新信息推翻时，用相同的 id 返回更新后的内容；
4. 用户明确说"不要记/忘掉"的内容不要抽取；
5. 没有值得记的信息时返回空列表，宁缺毋滥。"""


class MemoryManager(Protocol):
    """langmem create_memory_manager 返回的 Runnable 的最小协议（便于测试注入）。"""

    async def ainvoke(self, input: dict[str, Any], *args: Any, **kwargs: Any) -> Any: ...


class MemoryExtractor:
    """对话 → 结构化记忆 → store 的抽取合并器。"""

    def __init__(
        self,
        *,
        store: BaseStore,
        user_id: str,
        model: BaseChatModel | None = None,
        manager: MemoryManager | None = None,
        instructions: str = EXTRACT_INSTRUCTIONS,
        message_window: int = EXTRACT_MESSAGE_WINDOW,
    ) -> None:
        if manager is None and model is None:
            msg = "MemoryExtractor 需要 model 与 manager 至少提供一个"
            raise ValueError(msg)
        self._store = store
        self._user_id = user_id
        self._message_window = max(2, message_window)
        if manager is not None:
            self._manager: MemoryManager = manager
        else:
            from langmem import create_memory_manager

            self._manager = create_memory_manager(
                model,
                schemas=EXTRACT_SCHEMAS,
                instructions=instructions,
                enable_updates=True,
            )

    async def extract_and_merge(self, messages: Sequence[BaseMessage]) -> int:
        """对一段对话消息做抽取合并，返回写入/更新的记忆条数。"""
        window = list(messages)[-self._message_window :]
        if not window:
            return 0

        existing_items = await self._store.asearch(
            user_namespace(self._user_id), limit=EXISTING_LIMIT
        )
        existing: list[tuple[str, Any]] = []
        known: dict[str, tuple[tuple[str, ...], MemoryRecord]] = {}
        for item in existing_items:
            kind = kind_from_namespace(item.namespace)
            schema = KIND_SCHEMAS.get(kind)
            value = item.value if isinstance(item.value, dict) else {}
            content = str(value.get("content", "")).strip()
            if schema is None or not content:
                continue
            keywords_raw = value.get("keywords") or []
            keywords = (
                [str(word) for word in keywords_raw] if isinstance(keywords_raw, list) else []
            )
            try:
                instance = schema(content=content, keywords=keywords)
            except ValidationError:
                continue
            existing.append((item.key, instance))
            record = MemoryRecord(content=content)
            record["keywords"] = keywords
            created_at = value.get("created_at")
            if isinstance(created_at, str):
                record["created_at"] = created_at
            known[item.key] = (item.namespace, record)

        results = await self._manager.ainvoke({"messages": window, "existing": existing})
        written = 0
        for extracted in results or []:
            if await self._merge_one(extracted, known):
                written += 1
        if written:
            logger.info(
                "memory_extracted",
                user_id=self._user_id,
                written=written,
                messages=len(window),
            )
        return written

    async def _merge_one(
        self, extracted: Any, known: dict[str, tuple[tuple[str, ...], MemoryRecord]]
    ) -> bool:
        """把一条 ExtractedMemory(id, content: BaseModel) 落库；返回是否写入。"""
        content_model = getattr(extracted, "content", None)
        if not isinstance(content_model, BaseModel):
            return False
        kind = SCHEMA_KINDS.get(type(content_model))
        if kind is None:
            return False
        text = str(getattr(content_model, "content", "")).strip()
        if not text:
            return False
        keywords_raw = getattr(content_model, "keywords", None) or []
        keywords = [str(word) for word in keywords_raw] if isinstance(keywords_raw, list) else []

        extracted_id = str(getattr(extracted, "id", "") or "")
        if extracted_id and extracted_id in known:
            namespace, old = known[extracted_id]
            record = build_record(
                kind=kind_from_namespace(namespace),
                content=text,
                keywords=keywords,
                source=SOURCE_EXTRACT,
                created_at=old.get("created_at"),
            )
            await self._store.aput(namespace, extracted_id, dict(record))
            return True

        key = extracted_id or uuid7()
        record = build_record(kind=kind, content=text, keywords=keywords, source=SOURCE_EXTRACT)
        await self._store.aput(memory_namespace(self._user_id, kind), key, dict(record))
        return True


__all__ = [
    "EXISTING_LIMIT",
    "EXTRACT_INSTRUCTIONS",
    "EXTRACT_MESSAGE_WINDOW",
    "MemoryExtractor",
    "MemoryManager",
]
