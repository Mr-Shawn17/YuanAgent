"""OpenAI 兼容 /v1/chat/completions（计划 §6.4）。

定位是"次要入口"，供 ChatBox/LobeChat 等现成客户端直连：
只输出文本增量，工具轨迹与审批交互折叠掉；遇到 HITL interrupt 时
输出占位提示（完整审批体验走 Web 前端 + /v1/chat）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt

from app.agents.builder import thread_config
from app.agents.state import initial_state_fields
from app.api.deps import get_runtime
from app.api.routes_chat import error_payload, schedule_post_run, text_of
from app.api.runtime import AgentRuntime, make_web_approval
from app.api.schemas import OpenAIChatRequest, OpenAIMessage
from app.api.sse import SSE_HEADERS
from app.core.errors import AppError, InputValidationError
from app.core.events import EventBus
from app.core.logging import get_logger
from app.middleware.telemetry import usage_of

logger = get_logger(__name__)
router = APIRouter()

# interrupt 挂起时给兼容客户端的占位文本（该轮不会继续执行）
INTERRUPT_NOTICE = "［本轮触发了需要人工审批的工具，已暂停执行；请在 Web 界面完成审批后重试。］"
# pump 队列里的 interrupt 哨兵（区别于正常的 (mode, chunk) 元组）
_INTERRUPT_SENTINEL = "__interrupt__"


async def _run_turn(
    runtime: AgentRuntime,
    *,
    session_id: str,
    user_id: str,
    message: str,
) -> AsyncIterator[tuple[str, Any]]:
    """跑一轮图，产出 ("delta", 文本) / ("usage", dict) / ("error", dict)。

    调用方必须已 runs.acquire(session_id)；本生成器在 finally 里释放。
    """
    bus = EventBus()  # 不订阅：兼容端点丢弃工具轨迹事件，只保留文本
    agent, permission_mw = runtime.build_run_agent(
        session_id=session_id,
        event_bus=bus,
        approval=make_web_approval(),
    )
    config = thread_config(session_id)
    graph_input = {
        "messages": [HumanMessage(content=message)],
        **initial_state_fields(
            session_id=session_id,
            user_id=user_id,
        ),
    }

    stream_q: asyncio.Queue[Any] = asyncio.Queue()

    async def _pump() -> None:
        try:
            async for item in agent.astream(
                graph_input,
                cast(RunnableConfig, config),
                stream_mode=["messages", "updates"],
            ):
                await stream_q.put(item)
        except GraphInterrupt:
            await stream_q.put((_INTERRUPT_SENTINEL, None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await stream_q.put(exc)
        finally:
            await stream_q.put(None)

    pump = asyncio.create_task(_pump())
    usage = {"in": 0, "out": 0}
    interrupted = False
    failed = False
    try:
        while True:
            item = await stream_q.get()
            if item is None:
                break
            if isinstance(item, Exception):
                failed = True
                yield ("error", error_payload(item))
                break
            mode, chunk = item
            if mode == _INTERRUPT_SENTINEL:
                interrupted = True
                continue
            if mode == "messages":
                message_chunk = chunk[0] if isinstance(chunk, tuple) else chunk
                if isinstance(message_chunk, AIMessageChunk):
                    delta = text_of(message_chunk.content)
                    if delta:
                        yield ("delta", delta)
                    used = usage_of(message_chunk)
                    usage["in"] += used.get("input_tokens", 0)
                    usage["out"] += used.get("output_tokens", 0)
            elif mode == "updates" and isinstance(chunk, dict) and "__interrupt__" in chunk:
                interrupted = True
        if interrupted:
            yield ("delta", INTERRUPT_NOTICE)
        yield ("usage", usage)
        if not failed and not interrupted:
            await schedule_post_run(runtime, agent, config)
    finally:
        if not pump.done():
            pump.cancel()
        await permission_mw.wait_pending_audits()
        runtime.runs.release(session_id)


def _last_user_text(messages: list[OpenAIMessage]) -> str:
    """OpenAI 多轮 messages 里取最后一条有效 user 输入（历史由 checkpoint 维护）。"""
    for item in reversed(messages):
        if item.role == "user":
            text = text_of(item.content).strip()
            if text:
                return text
    return ""


def _chunk_frame(
    completion_id: str,
    created: int,
    model: str,
    delta: str | None,
    *,
    finish: str | None = None,
) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta} if delta is not None else {},
                "finish_reason": finish,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    payload: OpenAIChatRequest,
    runtime: AgentRuntime = Depends(get_runtime),
) -> JSONResponse | StreamingResponse:
    """OpenAI 兼容聊天端点：stream=false 一次性 JSON，stream=true SSE 增量。"""
    message = _last_user_text(payload.messages)
    if not message:
        raise InputValidationError("messages 缺少有效的 user 输入")
    session_id = payload.session_id.strip() or uuid.uuid4().hex
    if not runtime.runs.acquire(session_id):
        raise AppError("该会话正在处理中，请稍后再试", code="conflict", status_code=409)

    user_id = runtime.user_id
    model_name = payload.model.strip() or runtime.settings.llm_model
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    turn = _run_turn(runtime, session_id=session_id, user_id=user_id, message=message)

    if not payload.stream:
        parts: list[str] = []
        usage: dict[str, int] = {"in": 0, "out": 0}
        failure: dict[str, Any] | None = None
        async for kind, value in turn:
            if kind == "delta":
                parts.append(cast(str, value))
            elif kind == "usage":
                usage = cast(dict[str, int], value)
            elif kind == "error":
                failure = cast(dict[str, Any], value)
        if failure is not None:
            status = int(failure.get("status", 500))
            return JSONResponse(
                status_code=status,
                content={"error": {"code": failure["code"], "message": failure["message"]}},
            )
        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "".join(parts)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": usage["in"],
                    "completion_tokens": usage["out"],
                    "total_tokens": usage["in"] + usage["out"],
                },
            }
        )

    async def _frames() -> AsyncIterator[str]:
        async for kind, value in turn:
            if kind == "delta":
                yield _chunk_frame(completion_id, created, model_name, cast(str, value))
            elif kind == "error":
                error = cast(dict[str, Any], value)
                body = {"error": {"code": error["code"], "message": error["message"]}}
                yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
        yield _chunk_frame(completion_id, created, model_name, None, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(_frames(), media_type="text/event-stream", headers=SSE_HEADERS)


__all__ = ["INTERRUPT_NOTICE", "router"]
