"""聊天主链路 HTTP 端点：SSE 流式对话、HITL 审批恢复、历史回放。

SSE 事件协议见 v1 计划 §6.3：每条 data: 一个 JSON 帧，
type ∈ session/token/tool_start/tool_end/approval_required/citation/
memory_recalled/usage/done/error。

HITL 流程：审批回调里 interrupt() 挂起图 → 流内发 approval_required（带 run_id）
→ 前端 POST /v1/chat/{run_id}/approve → Command(resume=布尔) 恢复同一线程。
resume 后 interrupt 之前的代码会重新执行（LangGraph 语义），因此重发的
approval_required（同 call_id）被 suppress_call_id 过滤，approval_decided
一律跳过（决定结果前端已从 approve 请求得知）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from app.agents.builder import thread_config
from app.agents.state import initial_state_fields
from app.api.deps import get_runtime
from app.api.runtime import AgentRuntime, make_web_approval
from app.api.schemas import ApproveRequest, ChatRequest, SessionMessage
from app.api.sse import SSE_HEADERS, sse_event
from app.core.errors import AppError, InputValidationError, NotFoundError
from app.core.events import AgentEvent, EventBus
from app.core.logging import get_logger
from app.db.message_sync import persist_new_messages
from app.middleware.telemetry import usage_of

logger = get_logger(__name__)
router = APIRouter()


def text_of(content: Any) -> str:
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


def error_payload(exc: Exception) -> dict[str, Any]:
    """异常 → {"code","message","status"}；AppError 保留原始语义，其余归为内部错误。"""
    if isinstance(exc, AppError):
        return {"code": exc.code, "message": exc.message, "status": exc.status_code}
    logger.error("chat_stream_failed", exc_info=exc)
    return {
        "code": "internal_error",
        "message": str(exc) or type(exc).__name__,
        "status": 500,
    }


async def schedule_post_run(
    runtime: AgentRuntime,
    agent: Any,
    config: dict[str, Any],
    *,
    before_messages: list[Any] | None = None,
    usage: dict[str, int] | None = None,
) -> None:
    """轮次成功结束后从 checkpoint 取全量消息，调度后台记忆抽取 + 同步业务表 messages。

    失败只记日志：副本写入与记忆抽取都不影响 checkpoint 权威数据。
    """
    try:
        state = await agent.aget_state(cast(RunnableConfig, config))
    except Exception:
        logger.warning("post_run_state_failed", exc_info=True)
        return
    messages = list((getattr(state, "values", None) or {}).get("messages") or [])
    runtime.schedule_extraction(messages)

    # 同步业务表 messages（冗余副本）：diff 本轮新增后写入
    usage = usage or {"in": 0, "out": 0}
    thread_id = str((cast(RunnableConfig, config).get("configurable") or {}).get("thread_id", ""))
    await persist_new_messages(
        runtime.database,
        session_id=thread_id,
        user_id=runtime.user_id,
        before_messages=list(before_messages or []),
        after_messages=messages,
        tokens_in=usage.get("in", 0),
        tokens_out=usage.get("out", 0),
    )


def _translate_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """EventBus 事件 dict → SSE 协议帧（§6.3 字段名）；返回 None 表示跳过该事件。"""
    kind = str(event.get("type", ""))
    if kind == "tool_start":
        return {
            "type": "tool_start",
            "call_id": event.get("call_id", ""),
            "tool": event.get("tool_name", ""),
            "tier": event.get("tier", ""),
            "origin": event.get("origin", "builtin"),
            "args": event.get("args") or {},
            "task_id": event.get("task_id", ""),
        }
    if kind == "tool_end":
        return {
            "type": "tool_end",
            "call_id": event.get("call_id", ""),
            "ok": bool(event.get("ok")),
            "preview": event.get("preview", ""),
            "ms": event.get("latency_ms", 0),
            "task_id": event.get("task_id", ""),
        }
    if kind == "fanout_start":
        return {
            "type": "fanout_start",
            "fanout_id": event.get("fanout_id", ""),
            "tasks": event.get("tasks") or [],
        }
    if kind == "fanout_task_start":
        return {
            "type": "fanout_task_start",
            "fanout_id": event.get("fanout_id", ""),
            "task_id": event.get("task_id", ""),
            "agent": event.get("agent", ""),
        }
    if kind == "fanout_task_end":
        return {
            "type": "fanout_task_end",
            "fanout_id": event.get("fanout_id", ""),
            "task_id": event.get("task_id", ""),
            "agent": event.get("agent", ""),
            "ok": bool(event.get("ok")),
            "ms": event.get("latency_ms", 0),
            "error": event.get("error", ""),
        }
    if kind == "fanout_end":
        return {
            "type": "fanout_end",
            "fanout_id": event.get("fanout_id", ""),
            "ok_count": event.get("ok_count", 0),
            "fail_count": event.get("fail_count", 0),
            "ms": event.get("total_ms", 0),
        }
    if kind == "citation":
        return {"type": "citation", "items": event.get("items") or []}
    if kind == "memory_recalled":
        return {"type": "memory_recalled", "count": event.get("count", 0)}
    # approval_required 由调用方缓冲（要等 interrupt 确认后附 run_id），
    # approval_decided / memory_extracted 不进 SSE 流。
    return None


def _interrupt_value(raw: Any) -> dict[str, Any]:
    """从 __interrupt__ 载荷或 GraphInterrupt 异常里取出 interrupt 值 dict。"""
    first = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
    value = getattr(first, "value", None)
    return value if isinstance(value, dict) else {}


async def _stream_run(
    runtime: AgentRuntime,
    *,
    session_id: str,
    user_id: str,
    graph_input: Any,
    title: str = "",
    suppress_call_id: str = "",
) -> AsyncIterator[str]:
    """执行一轮图并把过程转成 SSE 帧序列。

    事件双通道：EventBus（工具/审批/引用等业务事件）走内存队列，
    agent.astream(stream_mode=["messages","updates"]) 走 token 流与 interrupt 检测；
    业务事件优先排空，保证 tool_start 先于其后的 token 到达前端。
    调用方必须已 runs.acquire(session_id)，本生成器在 finally 里释放。
    """
    bus = EventBus()
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def _on_event(event: AgentEvent) -> None:
        events.put_nowait(event.to_dict())

    unsubscribe = bus.subscribe(_on_event)
    agent, permission_mw = runtime.build_run_agent(
        session_id=session_id,
        event_bus=bus,
        approval=make_web_approval(),
    )
    config = thread_config(session_id)

    session_frame: dict[str, Any] = {"type": "session", "session_id": session_id}
    if title:
        session_frame["title"] = title
    yield sse_event(session_frame)

    stream_q: asyncio.Queue[Any] = asyncio.Queue()
    interrupted: dict[str, Any] | None = None

    # 本轮开始前的消息快照：用于轮次结束后 diff 出新增消息写入业务表
    before_messages: list[Any] = []
    try:
        before_state = await agent.aget_state(cast(RunnableConfig, config))
        before_messages = list(
            (getattr(before_state, "values", None) or {}).get("messages") or []
        )
    except Exception:
        logger.warning("pre_run_state_failed", exc_info=True)

    async def _pump() -> None:
        nonlocal interrupted
        try:
            async for item in agent.astream(
                graph_input,
                cast(RunnableConfig, config),
                stream_mode=["messages", "updates"],
            ):
                await stream_q.put(item)
        except GraphInterrupt as exc:
            # 部分执行路径会把 interrupt 以异常形式抛出（与 updates 通道双保险）
            interrupted = _interrupt_value(getattr(exc, "interrupts", ()) or ())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await stream_q.put(exc)
        finally:
            await stream_q.put(None)

    pump = asyncio.create_task(_pump())
    usage = {"in": 0, "out": 0}
    pending_approval: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    stream_done = False
    try:
        while True:
            while not events.empty():
                event = events.get_nowait()
                if event.get("type") == "approval_required":
                    # resume 重放时同 call_id 的审批请求不再打扰前端
                    if event.get("call_id") != suppress_call_id:
                        pending_approval = event
                    continue
                frame = _translate_event(event)
                if frame is not None:
                    yield sse_event(frame)
            if stream_done:
                break
            item = await stream_q.get()
            if item is None:
                stream_done = True
                continue
            if isinstance(item, Exception):
                failure = error_payload(item)
                break
            mode, chunk = item
            if mode == "messages":
                message = chunk[0] if isinstance(chunk, tuple) else chunk
                if isinstance(message, AIMessageChunk):
                    delta = text_of(message.content)
                    if delta:
                        yield sse_event({"type": "token", "delta": delta})
                    used = usage_of(message)
                    usage["in"] += used.get("input_tokens", 0)
                    usage["out"] += used.get("output_tokens", 0)
            elif mode == "updates" and isinstance(chunk, dict) and "__interrupt__" in chunk:
                interrupted = _interrupt_value(chunk["__interrupt__"])

        if failure is not None:
            yield sse_event({"type": "error", **failure})
        elif interrupted is not None:
            run_id = runtime.runs.register_pending(session_id, user_id)
            approval = pending_approval or {}
            yield sse_event(
                {
                    "type": "approval_required",
                    "run_id": run_id,
                    "call_id": approval.get("call_id", ""),
                    "tool": approval.get("tool_name", ""),
                    "tier": approval.get("tier", ""),
                    "reason": approval.get("summary", ""),
                }
            )
        else:
            if usage["in"] or usage["out"]:
                yield sse_event(
                    {
                        "type": "usage",
                        "tokens_in": usage["in"],
                        "tokens_out": usage["out"],
                    }
                )
            await schedule_post_run(
                runtime,
                agent,
                config,
                before_messages=before_messages,
                usage=usage,
            )
        yield sse_event({"type": "done", "session_id": session_id})
    finally:
        if not pump.done():
            pump.cancel()
        unsubscribe()
        await permission_mw.wait_pending_audits()
        runtime.runs.release(session_id)


def _sse_response(frames: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        frames,
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/v1/chat")
async def chat(
    payload: ChatRequest,
    runtime: AgentRuntime = Depends(get_runtime),
) -> StreamingResponse:
    """主聊天端点：SSE 流式返回本轮生成过程（协议 §6.3）。"""
    message = payload.message.strip()
    if not message:
        raise InputValidationError("message 不能为空")
    session_id = payload.session_id.strip() or uuid.uuid4().hex
    user_id = payload.user_id.strip() or runtime.user_id
    if not runtime.runs.acquire(session_id):
        raise AppError("该会话正在处理中，请稍后再试", code="conflict", status_code=409)
    graph_input = {
        "messages": [HumanMessage(content=message)],
        **initial_state_fields(
            session_id=session_id,
            user_id=user_id,
        ),
    }
    title = " ".join(message.split())[:32]
    return _sse_response(
        _stream_run(
            runtime,
            session_id=session_id,
            user_id=user_id,
            graph_input=graph_input,
            title=title,
        )
    )


@router.post("/v1/chat/{run_id}/approve")
async def approve_run(
    run_id: str,
    payload: ApproveRequest,
    runtime: AgentRuntime = Depends(get_runtime),
) -> StreamingResponse:
    """审批回调：Command(resume=布尔) 恢复被 interrupt 挂起的图，继续 SSE 流式返回。"""
    pending = runtime.runs.pop_pending(run_id)
    if pending is None:
        raise NotFoundError("run_id 不存在或已过期，请重新发起对话")
    # 挂起的 run 在首个流结束时已 release，同线程正常不会冲突；
    # 万一冲突（并发重复 approve），pop 掉的 pending 直接作废，客户端可重发消息。
    if not runtime.runs.acquire(pending.thread_id):
        raise AppError("该会话正在处理中，请稍后再试", code="conflict", status_code=409)
    graph_input: Any = Command(resume=(payload.decision == "allow"))
    return _sse_response(
        _stream_run(
            runtime,
            session_id=pending.thread_id,
            user_id=pending.user_id,
            graph_input=graph_input,
            suppress_call_id=payload.call_id,
        )
    )


@router.get("/v1/sessions/{session_id}/messages")
async def session_messages(
    session_id: str,
    runtime: AgentRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    """回放会话历史（只含 user/assistant 文本，工具轨迹不回放）。"""
    entry = await runtime.checkpointer.aget_tuple(thread_config(session_id))
    if entry is None:
        raise NotFoundError("会话不存在")
    raw_messages = list((entry.checkpoint.get("channel_values") or {}).get("messages") or [])
    messages: list[SessionMessage] = []
    for message in raw_messages:
        if isinstance(message, HumanMessage):
            text = text_of(message.content).strip()
            if text:
                messages.append(SessionMessage(role="user", content=text))
        elif isinstance(message, AIMessage) and not message.tool_calls:
            text = text_of(message.content).strip()
            if text:
                messages.append(SessionMessage(role="assistant", content=text))
    return {
        "session_id": session_id,
        "messages": [item.model_dump() for item in messages],
    }


__all__ = [
    "error_payload",
    "router",
    "schedule_post_run",
    "text_of",
]
