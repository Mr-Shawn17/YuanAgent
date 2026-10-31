"""SSE 编码工具：把事件 dict 编成 `data: {...}\\n\\n` 帧（v1 计划 §6.3 协议）。"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

# 禁用代理缓冲，保证 token 级流式不被 nginx/浏览器攒批
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# SSE 心跳间隔：流空闲期间每 5 秒发一行注释帧（: ping），防止中间代理
# （Next dev rewrites / nginx / 浏览器）因长时间无数据而掐断连接——
# 典型触发场景：fan_out 并行子任务内 RAG rerank 重排耗时 15s+，期间无 token 输出。
# 注释行是 SSE 规范的一部分，浏览器与本项目前端解析器都会自动忽略，零侵入。
SSE_HEARTBEAT_SECONDS = 5.0

# 心跳注释帧（不以 data: 开头，任何标准 SSE 解析器都不会当事件分发）
SSE_HEARTBEAT_FRAME = ": ping\n\n"


def sse_event(payload: dict[str, Any]) -> str:
    """单个 SSE 帧：data 行 + 空行。ensure_ascii=False 让中文直出，省流量。"""
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def sse_raw(data: str) -> str:
    """OpenAI 兼容层的原始 data 帧（如 [DONE]）。"""
    return f"data: {data}\n\n"


def with_heartbeat(
    body: Any,
    *,
    interval: float = SSE_HEARTBEAT_SECONDS,
) -> Any:
    """给同步生成的 SSE body 生成器套一层异步心跳。

    用法（FastAPI StreamingResponse 场景）::

        async def gen(): ...
        return StreamingResponse(
            with_heartbeat(gen()),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    机制：原生成器产出一条立即转发；空闲超过 interval 秒，自动插入 ``: ping``
    注释帧保活。原生成器结束/抛错时心跳随之终止，不改变任何事件语义。

    实现注意：不能用 asyncio.wait_for 包 __anext__()——超时会取消底层协程，
    直接杀死上游生成器（后续事件全部丢失）。必须用 asyncio.wait（超时仅返回、
    不取消 pending 任务），把 __anext__ 任务跨心跳存活到真正完成。
    """
    interval = max(interval, 0.1)

    async def _wrapped() -> Any:
        agen = body.__aiter__()
        pending_next: asyncio.Task[Any] | None = None
        try:
            while True:
                if pending_next is None:
                    pending_next = asyncio.ensure_future(agen.__anext__())
                done, _ = await asyncio.wait({pending_next}, timeout=interval)
                if not done:
                    # 空闲超 interval 秒：发心跳，__anext__ 任务继续存活等待
                    yield SSE_HEARTBEAT_FRAME
                    continue
                task, pending_next = pending_next, None
                if task.cancelled():  # pragma: no cover - 外层取消场景
                    return
                exc = task.exception()
                if exc is not None:
                    if isinstance(exc, StopAsyncIteration):
                        return
                    raise exc
                yield task.result()
        finally:
            # 消费方断开（GeneratorExit）时回收在途任务与上游生成器
            if pending_next is not None:
                pending_next.cancel()
            with suppress(Exception):
                await agen.aclose()

    return _wrapped()


__all__ = [
    "SSE_HEADERS",
    "SSE_HEARTBEAT_FRAME",
    "SSE_HEARTBEAT_SECONDS",
    "sse_event",
    "sse_raw",
    "with_heartbeat",
]
