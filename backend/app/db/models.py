from __future__ import annotations

import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# LangGraph 通过 .setup() 自管的表：业务代码与 Alembic 一律不碰，避免迁移误删
LANGGRAPH_MANAGED_TABLES: frozenset[str] = frozenset(
    {
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "store",
        "store_migrations",
    }
)

# 工具审计的状态取值（安全矩阵里 status 列的合法域）
AUDIT_STATUS_OK = "ok"
AUDIT_STATUS_DENIED = "denied"
AUDIT_STATUS_ERROR = "error"

# 消息角色取值
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"
ROLE_SYSTEM = "system"


def utcnow() -> datetime:
    """统一的「 naive UTC 当前时间」。

    MySQL 的 DATETIME 不保存时区，混用 DB 端 now() 与 Python 本地时间会导致
    同一张表里时间基准不一致，所以业务表的时间戳全部由应用侧写入 naive UTC。
    """
    return datetime.now(UTC).replace(tzinfo=None)


def uuid7() -> str:
    """生成时间有序的 UUIDv7（48bit 毫秒时间戳 + 80bit 随机），用作会话主键。

    相比 uuid4，前缀单调递增让 InnoDB 主键索引顺序写入、范围扫描更友好。
    """
    timestamp_ms = time.time_ns() // 1_000_000
    raw = bytearray(secrets.token_bytes(16))
    raw[0:6] = timestamp_ms.to_bytes(6, byteorder="big")
    raw[6] = (raw[6] & 0x0F) | 0x70  # 高 4 位写入 version = 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # 高 2 位写入 variant = 10
    return str(uuid.UUID(bytes=bytes(raw)))


class Base(DeclarativeBase):
    """业务表基类。"""


class Session(Base):
    """会话元信息（对话正文的权威副本仍在 LangGraph checkpoint 里）。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    meta: Mapped[dict[str, Any]] = mapped_column("meta", JSON, nullable=False, default=dict)


class Message(Base):
    """消息的业务可读副本，便于列表展示、检索与统计（不参与模型上下文拼装）。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    citations: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    tool_calls: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    # 执行链路图（主 Agent → 子 Agent → 工具 → 汇总）的最终完成态快照，
    # 供历史回放渲染图谱（结构与前端 ExecutionGraphModel 对应，dict 形式存 JSON）
    execution_graph: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, index=True, nullable=False, default=utcnow
    )


class Device(Base):
    """本地执行器设备：设备令牌只存哈希，原文仅在签发时显示一次。

    user_id 当前为单用户默认值，多用户登录体系上线后按真实账号归属。
    """

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ToolAudit(Base):
    """工具调用审计：安全矩阵的落地凭证，read/write/danger 全量记录。"""

    __tablename__ = "tool_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    args_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    approved_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, index=True, nullable=False, default=utcnow
    )


__all__ = [
    "AUDIT_STATUS_DENIED",
    "AUDIT_STATUS_ERROR",
    "AUDIT_STATUS_OK",
    "LANGGRAPH_MANAGED_TABLES",
    "ROLE_ASSISTANT",
    "ROLE_SYSTEM",
    "ROLE_TOOL",
    "ROLE_USER",
    "Base",
    "Device",
    "Message",
    "Session",
    "ToolAudit",
    "utcnow",
    "uuid7",
]
