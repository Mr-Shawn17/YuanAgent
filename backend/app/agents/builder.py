from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncmy
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver
from langgraph.store.mysql.asyncmy import AsyncMyStore

from app.agents.state import AgentState
from app.core.config import AgentFileConfig, Settings, get_agent_config, get_settings
from app.core.logging import get_logger
from app.llm.factory import get_chat_model

logger = get_logger(__name__)

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

PROMPTS_DIR: Path = Path(__file__).parent / "prompts"
STRICT_MSGPACK_ENV = "LANGGRAPH_STRICT_MSGPACK"


def ensure_strict_msgpack() -> None:
    os.environ[STRICT_MSGPACK_ENV] = "true"


ensure_strict_msgpack()


@lru_cache(maxsize=1)
def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(PROMPTS_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )


def now_in(timezone: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return datetime.now()


def render_prompt(
    template_name: str,
    **context: Any,
) -> str:
    template = _jinja_env().get_template(template_name)
    return template.render(**context).strip()


def render_system_prompt(
    config: AgentFileConfig | None = None,
    *,
    session_id: str = "",
    user_id: str = "",
    notes: str = "",
    now: datetime | None = None,
) -> str:
    resolved = config or get_agent_config()
    persona = resolved.persona
    current = now or now_in(persona.timezone)
    # 优先从 Langfuse 拉取 system prompt（版本化 + label 发布/回滚）；
    # 拉取/编译失败时回退本地 system.j2，保证对话永不受 Prompt 服务影响。
    langfuse_rendered = _render_system_prompt_from_langfuse(
        resolved,
        session_id=session_id,
        user_id=user_id,
        notes=notes,
        current=current,
    )
    if langfuse_rendered is not None:
        return langfuse_rendered
    return render_prompt(
        "system.j2",
        persona=persona,
        now=current.strftime("%Y-%m-%d %H:%M %A"),
        session_id=session_id,
        user_id=user_id,
        notes=notes.strip(),
    )


def _render_system_prompt_from_langfuse(
    config: AgentFileConfig,
    *,
    session_id: str,
    user_id: str,
    notes: str,
    current: datetime,
) -> str | None:
    """从 Langfuse 拉取 system prompt（label=production）并编译；失败返回 None（回退本地）。

    Langfuse 文本 prompt 用 Mustache 风格 {{ var }}，不支持 Jinja2 的 {% if %}，
    故 notes 的「## 补充说明」段在此处由 Python 拼接，模板里只留 {{ notes }} 占位。
    """
    try:
        from app.observability import get_langfuse, is_enabled

        if not is_enabled():
            return None
        prompt = get_langfuse().get_prompt("agent-system", label="production", type="text")
        if prompt is None:
            return None
        persona = config.persona
        notes_block = ""
        if notes.strip():
            notes_block = f"\n## 补充说明\n{notes.strip()}"
        compiled = prompt.compile(
            name=persona.name,
            role=persona.role,
            style=persona.style,
            boundaries=persona.boundaries,
            timezone=persona.timezone,
            language=persona.language,
            now=current.strftime("%Y-%m-%d %H:%M %A"),
            session_id=session_id or "未命名",
            notes=notes_block,
        )
        return str(compiled).strip()
    except Exception:  # pragma: no cover - 任何异常都回退本地，绝不影响对话
        logger.warning("langfuse_prompt_render_failed", exc_info=True)
        return None


def append_system_fragment(request: ModelRequest, fragment: str) -> ModelRequest:
    """在 system prompt 末尾追加一个片段，返回新的 ModelRequest。

    ModelRequest.system_message 的类型是 SystemMessage | None（override 也只收 SystemMessage），
    而中间件想追加的是渲染出来的纯文本，所以统一在这里做
    「取纯文本 → 拼接 → 重建 SystemMessage」，persona 与 memory_recall 共用。
    """
    text = fragment.strip()
    if not text:
        return request
    current = request.system_message
    base = (current.text if current is not None else "").rstrip()
    combined = f"{base}\n\n{text}" if base else text
    return request.override(system_message=SystemMessage(content=combined))


def thread_config(session_id: str, **extra: Any) -> dict[str, Any]:
    config: dict[str, Any] = {"configurable": {"thread_id": session_id}}
    config.update(extra)
    return config


def session_title(messages: Sequence[Any], limit: int = 32) -> str:
    """从会话消息里派生一个短标题：取首条用户消息的前 limit 个字符。"""
    for message in messages:
        if isinstance(message, HumanMessage):
            text = message.content if isinstance(message.content, str) else str(message.content)
            text = " ".join(text.split())
            return text[:limit] + ("…" if len(text) > limit else "")
    return "(无用户消息)"


async def list_sessions(checkpointer: Any, *, limit: int = 400) -> list[dict[str, Any]]:
    """跨线程列出历史会话（CLI /sessions 与 API GET /v1/sessions 共用）。

    MySQL Saver 的 alist() 要求 config 为位置参数，传 None 表示跨线程列出全部
    checkpoint；按 thread_id 分桶取最新一条，从 channel_values.messages 派生标题。
    """
    buckets: dict[str, dict[str, Any]] = {}
    async for entry in checkpointer.alist(None, limit=limit):
        config = entry.config or {}
        configurable = config.get("configurable") or {}
        thread_id = configurable.get("thread_id")
        if not thread_id:
            continue
        checkpoint = entry.checkpoint or {}
        ts = str(checkpoint.get("ts", ""))
        current = buckets.get(thread_id)
        if current is None or ts > current["ts"]:
            buckets[thread_id] = {"thread_id": thread_id, "ts": ts, "checkpoint": checkpoint}

    sessions: list[dict[str, Any]] = []
    for item in buckets.values():
        channel_values = item["checkpoint"].get("channel_values") or {}
        messages = list(channel_values.get("messages") or [])
        ts_raw = item["ts"]
        try:
            ts_display = (
                datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
            )
        except ValueError:
            ts_display = ts_raw[:16]
        sessions.append(
            {
                "thread_id": item["thread_id"],
                "updated_at": ts_display,
                "message_count": len(messages),
                "title": session_title(messages),
            }
        )
    sessions.sort(key=lambda row: row["updated_at"], reverse=True)
    return sessions


def build_agent(
    *,
    model: BaseChatModel | None = None,
    tools: Sequence[BaseTool] | None = None,
    middleware: Sequence[Any] | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    store: Any | None = None,
    settings: Settings | None = None,
    agent_config: AgentFileConfig | None = None,
    session_id: str = "",
    user_id: str = "",
    notes: str = "",
) -> CompiledStateGraph[Any, Any, Any, Any]:
    ensure_strict_msgpack()
    resolved_settings = settings or get_settings()
    resolved_config = agent_config or get_agent_config()
    resolved_model = model or get_chat_model("main", settings=resolved_settings)

    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "tools": list(tools or []),
        "system_prompt": render_system_prompt(
            resolved_config,
            session_id=session_id,
            user_id=user_id or resolved_settings.default_user_id,
            notes=notes,
        ),
        "state_schema": AgentState,
    }
    if middleware:
        kwargs["middleware"] = list(middleware)
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    if store is not None:
        kwargs["store"] = store

    create = cast(Any, create_agent)
    return cast("CompiledStateGraph[Any, Any, Any, Any]", create(**kwargs))


@asynccontextmanager
async def mysql_checkpointer(
    settings: Settings | None = None,
) -> AsyncIterator[AsyncMySaver]:
    """打开 MySQL checkpointer（LangGraph 自管表，首次自动 setup 建表）。

    显式建 asyncmy 连接而非用 from_conn_string 解析连接串：后者的
    urlparse().password 不做百分号解码，密码含 @ 等字符会认证失败。

    驱动选 asyncmy 而非 aiomysql：aiomysql 依赖的 pymysql>=1.1 把
    escape_bytes_prefixed 改成了哨兵字符串，写入 bytes 参数（checkpoint
    的 msgpack blob）必抛 TypeError，上游尚未修复。
    """
    resolved = settings or get_settings()
    conn = await asyncmy.connect(db=resolved.mysql_database, **resolved.mysql_connect_kwargs)
    try:
        saver = AsyncMySaver(conn)
        await saver.setup()
        yield saver
    finally:
        conn.close()


@asynccontextmanager
async def mysql_store(settings: Settings | None = None) -> AsyncIterator[AsyncMyStore]:
    """打开 MySQL 长期记忆 store（LangGraph 自管表，首次自动 setup 建表）。"""
    resolved = settings or get_settings()
    conn = await asyncmy.connect(db=resolved.mysql_database, **resolved.mysql_connect_kwargs)
    try:
        store = AsyncMyStore(conn)
        await store.setup()
        yield store
    finally:
        conn.close()


__all__ = [
    "append_system_fragment",
    "build_agent",
    "ensure_strict_msgpack",
    "list_sessions",
    "mysql_checkpointer",
    "mysql_store",
    "now_in",
    "render_prompt",
    "render_system_prompt",
    "session_title",
    "thread_config",
]
