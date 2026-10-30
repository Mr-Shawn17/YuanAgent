"""SSE 编码工具：把事件 dict 编成 `data: {...}\\n\\n` 帧（v1 计划 §6.3 协议）。"""

from __future__ import annotations

import json
from typing import Any

# 禁用代理缓冲，保证 token 级流式不被 nginx/浏览器攒批
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def sse_event(payload: dict[str, Any]) -> str:
    """单个 SSE 帧：data 行 + 空行。ensure_ascii=False 让中文直出，省流量。"""
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def sse_raw(data: str) -> str:
    """OpenAI 兼容层的原始 data 帧（如 [DONE]）。"""
    return f"data: {data}\n\n"


__all__ = ["SSE_HEADERS", "sse_event", "sse_raw"]
