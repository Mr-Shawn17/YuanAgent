from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, cast

from app.core.logging import get_logger

logger = get_logger(__name__)

# fan_out 并行子任务的归属标记：子任务协程内设置，工具事件发布时读取。
# 并行 gather 时各协程有独立 context，互不串扰；主图流程（未设置）为空串。
_current_task_id: ContextVar[str] = ContextVar("fanout_current_task_id", default="")


@dataclass(frozen=True)
class AgentEvent:
    """事件基类。

    `type` 与 v1 计划 §6.3 SSE 事件协议的 type 字段一一对应，
    `to_dict()` 供 P5' 的 FastAPI SSE 直接做 JSON 序列化。
    """

    @property
    def type(self) -> str:
        return "event"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = asdict(self)
        data["type"] = self.type
        return data


@dataclass(frozen=True)
class ToolStartEvent(AgentEvent):
    """工具调用开始：args 必须是脱敏后的参数（redact_args 的输出）。"""

    call_id: str
    tool_name: str
    tier: str
    args: dict[str, Any] = field(default_factory=dict)
    origin: str = "builtin"
    # fan_out 并行子任务归属（B+ 粒度）：为空表示主图流程的工具调用
    task_id: str = ""

    @property
    def type(self) -> str:
        return "tool_start"


@dataclass(frozen=True)
class ApprovalRequiredEvent(AgentEvent):
    """write/danger 档工具需要用户确认（HITL）。"""

    call_id: str
    tool_name: str
    tier: str
    summary: str = ""

    @property
    def type(self) -> str:
        return "approval_required"


@dataclass(frozen=True)
class ApprovalDecidedEvent(AgentEvent):
    """审批结果：approved=False 时工具不会被执行。"""

    call_id: str
    tool_name: str
    approved: bool

    @property
    def type(self) -> str:
        return "approval_decided"


@dataclass(frozen=True)
class ToolEndEvent(AgentEvent):
    """工具调用结束：ok=False 时 preview 为错误摘要。"""

    call_id: str
    tool_name: str
    ok: bool
    latency_ms: int = 0
    preview: str = ""
    # fan_out 并行子任务归属（B+ 粒度）：为空表示主图流程的工具调用
    task_id: str = ""

    @property
    def type(self) -> str:
        return "tool_end"


@dataclass(frozen=True)
class MemoryRecalledEvent(AgentEvent):
    """本轮注入了哪些长期记忆（P4'）：items 为 kind/content 摘要列表。"""

    user_id: str
    count: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def type(self) -> str:
        return "memory_recalled"


@dataclass(frozen=True)
class MemoryExtractedEvent(AgentEvent):
    """后台记忆抽取完成（P4'）：written 为写入/更新的条数。"""

    user_id: str
    written: int = 0

    @property
    def type(self) -> str:
        return "memory_extracted"


@dataclass(frozen=True)
class CitationsEvent(AgentEvent):
    """RAG 引用列表：items 为 §7.2 KnowledgeChunk 契约的 dict 形式。"""

    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def type(self) -> str:
        return "citation"


@dataclass(frozen=True)
class FanoutStartEvent(AgentEvent):
    """fan_out 并行批次开始：tasks 为 [{task_id, agent, task}]，前端立即渲染等待节点。"""

    fanout_id: str
    tasks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def type(self) -> str:
        return "fanout_start"


@dataclass(frozen=True)
class FanoutTaskStartEvent(AgentEvent):
    """fan_out 批次内单个子任务开始执行。"""

    fanout_id: str
    task_id: str
    agent: str

    @property
    def type(self) -> str:
        return "fanout_task_start"


@dataclass(frozen=True)
class FanoutTaskEndEvent(AgentEvent):
    """fan_out 批次内单个子任务结束：ok=False 时 error 为失败原因摘要。"""

    fanout_id: str
    task_id: str
    agent: str
    ok: bool
    latency_ms: int = 0
    error: str = ""

    @property
    def type(self) -> str:
        return "fanout_task_end"


@dataclass(frozen=True)
class FanoutEndEvent(AgentEvent):
    """fan_out 并行批次整体结束（全部子任务完成或失败）。"""

    fanout_id: str
    ok_count: int = 0
    fail_count: int = 0
    total_ms: int = 0

    @property
    def type(self) -> str:
        return "fanout_end"


@dataclass(frozen=True)
class DelegateStartEvent(AgentEvent):
    """串行委派（delegate_to_<name>）开始：把单个任务委派给指定子 Agent。

    与 fan_out 的区别：单任务、串行、无并行批次概念；前端据此画成
    「主 Agent → 子 Agent」的独立节点（与 fan_out 并行图并列）。
    """

    delegate_id: str
    agent: str
    task: str = ""

    @property
    def type(self) -> str:
        return "delegate_start"


@dataclass(frozen=True)
class DelegateEndEvent(AgentEvent):
    """串行委派结束：ok=False 时 error 为失败原因摘要。"""

    delegate_id: str
    agent: str
    ok: bool
    latency_ms: int = 0
    error: str = ""

    @property
    def type(self) -> str:
        return "delegate_end"


# 事件总线不依赖 asyncio.Queue：CLI 同进程直接回调，P5' SSE 再按需加队列适配层
EventHandler = Callable[[AgentEvent], "Awaitable[None] | None"]


class EventBus:
    """轻量进程内事件总线：中间件/工具发布，CLI 与 SSE 订阅渲染。

    - 处理器可以是同步或异步函数，异常只记日志，绝不影响对话主流程；
    - subscribe 返回退订函数，方便 CLI 按轮订阅/退订；
    - publish 按订阅顺序串行执行，保证事件渲染顺序与发生顺序一致。
    """

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        self._handlers.append(handler)

        def _unsubscribe() -> None:
            try:
                self._handlers.remove(handler)
            except ValueError:
                pass

        return _unsubscribe

    async def publish(self, event: AgentEvent) -> None:
        for handler in list(self._handlers):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning(
                    "event_handler_failed",
                    event_type=event.type,
                    exc_info=True,
                )


__all__ = [
    "AgentEvent",
    "ApprovalDecidedEvent",
    "ApprovalRequiredEvent",
    "CitationsEvent",
    "DelegateEndEvent",
    "DelegateStartEvent",
    "EventBus",
    "EventHandler",
    "FanoutEndEvent",
    "FanoutStartEvent",
    "FanoutTaskEndEvent",
    "FanoutTaskStartEvent",
    "MemoryExtractedEvent",
    "MemoryRecalledEvent",
    "ToolEndEvent",
    "ToolStartEvent",
    "current_task_id",
    "make_event_logger",
]


def make_event_logger(
    *,
    session_id: str = "",
    user_id: str = "",
) -> EventHandler:
    """生成一个事件日志处理器：把关键业务事件结构化写入运行日志。

    供 CLI 与 SSE 两条链路复用——订阅到 EventBus 后，工具调用、子 Agent
    拆分（fan_out/delegate）等事件会自动落盘，便于事后排查完整调用链。
    用户问题本身由入口层（routes_chat.chat / cli）单独记一条，本处理器只
    负责过程事件，避免重复。

    记录的事件类型与字段（日志量已控制，不落大段 args/preview 全文）：
    - tool_start / tool_end：工具名、档位、来源、子任务、耗时、成败
    - fanout_start / fanout_task_start / fanout_task_end / fanout_end：子 Agent 拆分粒度
    - approval_required / approval_decided：人工审批触发点（write/danger 档）

    该处理器永不抛异常（异常仅记 warning），绝不影响对话主流程。
    """

    def _log(event: AgentEvent) -> None:
        try:
            etype = event.type
            if etype == "tool_start":
                ev = cast(ToolStartEvent, event)
                logger.info(
                    "tool_call_start",
                    session_id=session_id,
                    user_id=user_id,
                    tool=ev.tool_name,
                    tier=ev.tier,
                    origin=ev.origin,
                    task_id=ev.task_id or None,
                )
            elif etype == "tool_end":
                ev2 = cast(ToolEndEvent, event)
                logger.info(
                    "tool_call_end",
                    session_id=session_id,
                    user_id=user_id,
                    tool=ev2.tool_name,
                    ok=ev2.ok,
                    latency_ms=ev2.latency_ms,
                    task_id=ev2.task_id or None,
                    preview=(ev2.preview or "")[:120] if not ev2.ok else None,
                )
            elif etype == "fanout_start":
                ev3 = cast(FanoutStartEvent, event)
                agents = [t.get("agent") for t in ev3.tasks]
                logger.info(
                    "fanout_start",
                    session_id=session_id,
                    user_id=user_id,
                    fanout_id=ev3.fanout_id,
                    task_count=len(ev3.tasks),
                    agents=agents,
                )
            elif etype == "fanout_task_end":
                ev4 = cast(FanoutTaskEndEvent, event)
                logger.info(
                    "fanout_task_end",
                    session_id=session_id,
                    user_id=user_id,
                    fanout_id=ev4.fanout_id,
                    agent=ev4.agent,
                    ok=ev4.ok,
                    latency_ms=ev4.latency_ms,
                    error=(ev4.error or None) if not ev4.ok else None,
                )
            elif etype == "fanout_end":
                ev5 = cast(FanoutEndEvent, event)
                logger.info(
                    "fanout_end",
                    session_id=session_id,
                    user_id=user_id,
                    fanout_id=ev5.fanout_id,
                    ok_count=ev5.ok_count,
                    fail_count=ev5.fail_count,
                    total_ms=ev5.total_ms,
                )
            elif etype == "approval_required":
                ev6 = cast(ApprovalRequiredEvent, event)
                logger.info(
                    "approval_required",
                    session_id=session_id,
                    user_id=user_id,
                    tool=ev6.tool_name,
                    tier=ev6.tier,
                )
            elif etype == "approval_decided":
                ev7 = cast(ApprovalDecidedEvent, event)
                logger.info(
                    "approval_decided",
                    session_id=session_id,
                    user_id=user_id,
                    tool=ev7.tool_name,
                    approved=ev7.approved,
                )
            elif etype == "delegate_start":
                ev8 = cast(DelegateStartEvent, event)
                logger.info(
                    "delegate_start",
                    session_id=session_id,
                    user_id=user_id,
                    delegate_id=ev8.delegate_id,
                    agent=ev8.agent,
                    task=(ev8.task or "")[:120] or None,
                )
            elif etype == "delegate_end":
                ev9 = cast(DelegateEndEvent, event)
                logger.info(
                    "delegate_end",
                    session_id=session_id,
                    user_id=user_id,
                    delegate_id=ev9.delegate_id,
                    agent=ev9.agent,
                    ok=ev9.ok,
                    latency_ms=ev9.latency_ms,
                    error=(ev9.error or None) if not ev9.ok else None,
                )
        except Exception:
            logger.warning("event_logger_failed", event_type=etype, exc_info=True)

    return _log


def current_task_id() -> str:
    """读取当前协程的 fan_out 子任务标记（未在子任务协程内时为空串）。"""
    return _current_task_id.get()


def set_current_task_id(task_id: str) -> object:
    """在当前协程 context 内设置子任务标记，返回 token 供恢复。"""
    return _current_task_id.set(task_id)


def reset_current_task_id(token: object) -> None:
    """恢复 set_current_task_id 之前的标记状态。"""
    _current_task_id.reset(token)  # type: ignore[arg-type]
