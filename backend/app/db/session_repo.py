from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ROLE_USER, Message, Session, ToolAudit, utcnow, uuid7

TITLE_MAX_CHARS = 40
UNKNOWN_USER = "unknown"


def derive_title(content: str, *, limit: int = TITLE_MAX_CHARS) -> str:
    """用首条用户消息派生会话标题：压平空白后截断。"""
    text = " ".join(content.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


class SessionRepo:
    """会话 / 消息 / 工具审计的读写入口。

    只 flush 不 commit：事务边界由 `Database.session()` 统一掌握。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_session(
        self, *, session_id: str | None = None, user_id: str, title: str = ""
    ) -> Session:
        record = Session(id=session_id or uuid7(), user_id=user_id, title=title)
        self._db.add(record)
        await self._db.flush()
        return record

    async def get_session(self, session_id: str) -> Session | None:
        return await self._db.get(Session, session_id)

    async def touch_session(self, session_id: str, *, user_id: str) -> Session:
        """确保会话行存在；不存在则补建（老会话第一次写业务表时会走到）。"""
        record = await self.get_session(session_id)
        if record is None:
            return await self.create_session(session_id=session_id, user_id=user_id or UNKNOWN_USER)
        return record

    async def list_sessions(
        self,
        *,
        user_id: str | None = None,
        include_archived: bool = False,
        limit: int = 20,
    ) -> Sequence[Session]:
        stmt = select(Session)
        if user_id:
            stmt = stmt.where(Session.user_id == user_id)
        if not include_archived:
            stmt = stmt.where(Session.archived.is_(False))
        stmt = stmt.order_by(Session.updated_at.desc()).limit(limit)
        return (await self._db.scalars(stmt)).all()

    async def set_title(self, session_id: str, title: str) -> None:
        await self._db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(title=title[:255], updated_at=utcnow())
        )

    async def set_archived(self, session_id: str, *, archived: bool = True) -> None:
        await self._db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(archived=archived, updated_at=utcnow())
        )

    async def set_meta(self, session_id: str, key: str, value: Any, *, user_id: str = "") -> None:
        """写入会话 meta 的一个键（如 workspace）；会话不存在时补建。"""
        record = await self.touch_session(session_id, user_id=user_id or UNKNOWN_USER)
        meta = dict(record.meta or {})
        meta[key] = value
        record.meta = meta
        record.updated_at = utcnow()
        await self._db.flush()

    async def add_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        citations: list[Any] | None = None,
        tool_calls: list[Any] | None = None,
        execution_graph: dict[str, Any] | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        user_id: str = "",
    ) -> Message:
        """写入一条消息，并同步维护会话的计数、活跃时间与标题。"""
        record = await self.touch_session(session_id, user_id=user_id)
        message = Message(
            session_id=session_id,
            role=role,
            content=content,
            citations=citations,
            tool_calls=tool_calls,
            execution_graph=execution_graph,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        self._db.add(message)

        now = utcnow()
        record.message_count = int(record.message_count or 0) + 1
        record.last_message_at = now
        record.updated_at = now
        if role == ROLE_USER and not record.title.strip():
            record.title = derive_title(content)
        await self._db.flush()
        return message

    async def list_messages(
        self, session_id: str, *, limit: int = 200, before_id: int | None = None
    ) -> Sequence[Message]:
        """按时间正序返回最近 limit 条消息（before_id 用于向上翻页）。"""
        stmt = select(Message).where(Message.session_id == session_id)
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)
        stmt = stmt.order_by(Message.id.desc()).limit(limit)
        rows = (await self._db.scalars(stmt)).all()
        return list(reversed(rows))

    async def record_tool_audit(
        self,
        *,
        session_id: str,
        tool_name: str,
        tier: str,
        status: str,
        args_redacted: dict[str, Any] | None = None,
        approved_by_user: bool = False,
        latency_ms: int = 0,
        error_message: str | None = None,
    ) -> ToolAudit:
        record = ToolAudit(
            session_id=session_id,
            tool_name=tool_name,
            tier=tier,
            status=status,
            args_redacted=dict(args_redacted or {}),
            approved_by_user=approved_by_user,
            latency_ms=latency_ms,
            error_message=error_message,
        )
        self._db.add(record)
        await self._db.flush()
        return record

    async def list_tool_audits(
        self, *, session_id: str | None = None, limit: int = 20
    ) -> Sequence[ToolAudit]:
        stmt = select(ToolAudit)
        if session_id:
            stmt = stmt.where(ToolAudit.session_id == session_id)
        stmt = stmt.order_by(ToolAudit.id.desc()).limit(limit)
        return (await self._db.scalars(stmt)).all()


__all__ = ["TITLE_MAX_CHARS", "UNKNOWN_USER", "SessionRepo", "derive_title"]
