"""把一轮对话里新增的 user/assistant 消息同步写入业务表 messages。

业务表 messages 是「便于列表展示、检索与统计」的可读副本（权威副本仍是
LangGraph checkpoint）。本模块在轮次结束后，对比 checkpoint 前后消息列表，
把本轮新增的 HumanMessage / AIMessage 落库，tokens 用量记在最后一条
assistant 消息上（用量是整轮累计，粒度到轮即可）。

写入失败只记日志、绝不阻塞对话主链路——副本丢失不影响 checkpoint 权威数据。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.core.logging import get_logger
from app.db.engine import Database
from app.db.models import ROLE_ASSISTANT, ROLE_USER
from app.db.session_repo import SessionRepo

logger = get_logger(__name__)


def _text_of(content: Any) -> str:
    """从消息 content（str 或多模态块列表）里提取纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _citations_of(message: Any) -> list[Any] | None:
    """从消息的附加字段里提取引用（若存在），供业务表 JSON 列存储。"""
    citations = getattr(message, "citations", None)
    if isinstance(citations, list) and citations:
        return [dict(c) if isinstance(c, dict) else c for c in citations]
    return None


def diff_new_rows(
    *,
    before_messages: list[Any],
    after_messages: list[Any],
    tokens_in: int = 0,
    tokens_out: int = 0,
    execution_graph: dict[str, Any] | None = None,
) -> list[tuple[str, str, list[Any] | None, dict[str, Any] | None, int | None, int | None]]:
    """对比前后消息列表，产出本轮新增行。

    每行 (role, text, citations, execution_graph, tokens_in, tokens_out)；
    只取 HumanMessage / AIMessage 文本，跳过 tool/system/空内容；
    tokens 用量与执行链路图谱记在最后一条 assistant 消息上。
    """
    before_len = len(before_messages)
    new_messages = after_messages[before_len:]
    rows: list[
        tuple[str, str, list[Any] | None, dict[str, Any] | None, int | None, int | None]
    ] = []
    for message in new_messages:
        if isinstance(message, HumanMessage):
            text = _text_of(message.content)
            if text:
                rows.append((ROLE_USER, text, None, None, None, None))
        elif isinstance(message, AIMessage):
            text = _text_of(message.content)
            if text:
                rows.append((ROLE_ASSISTANT, text, _citations_of(message), None, None, None))
    if not rows:
        return []

    # tokens 用量与执行链路图谱记在最后一条 assistant 消息上（整轮累计，粒度到轮）；
    # 无用量数据（都为 0）时 tokens 保持 None，避免污染成本字段。
    has_payload = bool(tokens_in or tokens_out or execution_graph)
    if has_payload:
        for i in range(len(rows) - 1, -1, -1):
            if rows[i][0] == ROLE_ASSISTANT:
                role, text, citations, _graph, _tin, _tout = rows[i]
                tin = tokens_in if tokens_in else None
                tout = tokens_out if tokens_out else None
                rows[i] = (role, text, citations, execution_graph, tin, tout)
                break
    return rows


async def persist_new_messages(
    database: Database | None,
    *,
    session_id: str,
    user_id: str,
    before_messages: list[Any],
    after_messages: list[Any],
    tokens_in: int = 0,
    tokens_out: int = 0,
    execution_graph: dict[str, Any] | None = None,
) -> int:
    """把 after 相对 before 新增的 user/assistant 消息写入业务表，返回写入条数。

    database 为 None（离线/测试）时直接跳过，返回 0。
    """
    if database is None:
        return 0
    rows = diff_new_rows(
        before_messages=before_messages,
        after_messages=after_messages,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        execution_graph=execution_graph,
    )
    if not rows:
        return 0

    written = 0
    try:
        async with database.session() as session:
            repo = SessionRepo(session)
            for role, text, citations, graph, tin, tout in rows:
                await repo.add_message(
                    session_id,
                    role=role,
                    content=text,
                    citations=citations,
                    execution_graph=graph,
                    tokens_in=tin,
                    tokens_out=tout,
                    user_id=user_id,
                )
                written += 1
    except Exception:
        logger.warning("message_sync_failed", session_id=session_id, exc_info=True)
        return 0
    return written


__all__ = ["diff_new_rows", "persist_new_messages"]
