"""Langfuse 客户端封装与 trace 元数据工厂。

基于 langfuse v5（OpenTelemetry 架构）：
- 客户端用 Langfuse(host=, public_key=, secret_key=) 显式初始化（本项目自托管）；
- session_id / user_id / trace_name 通过 propagate_attributes 上下文注入；
- CallbackHandler 无参构造（用全局客户端），或传 trace_context 做分布式追踪；
- 未启用 / 未安装时全部优雅降级为 no-op，绝不阻塞对话主链路。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# langfuse 是可选依赖：未安装时本模块所有函数降级为 no-op，不影响主链路
try:  # pragma: no cover - 依赖是否安装取决于部署环境
    from langfuse import Langfuse, propagate_attributes
    from langfuse.langchain import CallbackHandler

    _LANGFUSE_AVAILABLE = True
except ImportError:  # pragma: no cover
    _LANGFUSE_AVAILABLE = False
    from contextlib import nullcontext

    class Langfuse:  # type: ignore[no-redef]
        pass

    class CallbackHandler:  # type: ignore[no-redef]
        pass

    def _propagate_attributes_noop(*args: Any, **kwargs: Any) -> Any:
        return nullcontext()

    # 变量赋值而非函数重定义：mypy 不再要求与真实签名逐字一致
    propagate_attributes = _propagate_attributes_noop


class _NullLangfuse:
    """langfuse 不可用/未启用时的 no-op 客户端，所有方法安全吞掉。"""

    def __getattr__(self, _name: str) -> Any:
        def _noop(*args: Any, **kwargs: Any) -> Any:
            return None

        return _noop

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


def is_enabled() -> bool:
    """langfuse 是否应启用：配置开关 + 密钥齐全 + SDK 已安装。"""
    if not _LANGFUSE_AVAILABLE:
        return False
    settings = get_settings()
    return bool(
        settings.langfuse_enabled
        and settings.langfuse_public_key.get_secret_value()
        and settings.langfuse_secret_key.get_secret_value()
    )


@lru_cache(maxsize=1)
def get_langfuse() -> Any:
    """返回单例 langfuse 客户端；未启用/未安装时返回 no-op。"""
    if not is_enabled():
        return _NullLangfuse()
    settings = get_settings()
    try:
        client = Langfuse(
            host=settings.langfuse_host,
            public_key=settings.langfuse_public_key.get_secret_value(),
            secret_key=settings.langfuse_secret_key.get_secret_value(),
        )
    except Exception as exc:  # pragma: no cover - 配置错误时降级
        logger.warning("langfuse_init_failed", error=exc.__class__.__name__)
        return _NullLangfuse()
    return client


def make_callbacks(*, session_id: str = "", user_id: str = "", trace_id: str = "") -> list[Any]:
    """构造传给 LangChain/LangGraph 的 Langfuse CallbackHandler 列表。

    未启用时返回空列表（等价于无观测）。CallbackHandler 用全局客户端，整条链
    （模型调用、工具调用、子图）自动捕获为嵌套 trace/span。

    trace_id 非空时通过 trace_context 传入，实现分布式追踪（跨进程串同一条 trace）。
    session_id/user_id 通过 propagate_attributes 上下文注入（见 langfuse_context）。
    """
    if not is_enabled():
        return []
    try:
        # 先确保客户端已按本项目配置注册到全局单例（Langfuse(host, public_key, secret_key)），
        # 这样 CallbackHandler() 内部的 get_client() 才能复用同一个客户端，
        # 而不是退回到读环境变量的默认实例。
        get_langfuse()
        kwargs: dict[str, Any] = {}
        if trace_id:
            kwargs["trace_context"] = {"trace_id": trace_id}
        handler = CallbackHandler(**kwargs)
    except Exception as exc:  # pragma: no cover
        logger.warning("langfuse_handler_failed", error=exc.__class__.__name__)
        return []
    return [handler]


@contextmanager
def langfuse_context(*, session_id: str = "", user_id: str = "") -> Iterator[None]:
    """在上下文内给所有观测注入 session_id / user_id（trace 根元数据）。

    包裹 agent.astream 的调用即可让本轮所有 trace/span 带上会话与用户标识，
    便于面板按 session/user 过滤与做会话回放。未启用时为 no-op。
    """
    if not is_enabled():
        yield
        return
    try:
        with propagate_attributes(session_id=session_id or None, user_id=user_id or None):
            yield
    except Exception as exc:  # pragma: no cover
        logger.warning("langfuse_context_failed", error=exc.__class__.__name__)
        yield


def langfuse_trace_metadata(*, session_id: str, user_id: str) -> dict[str, Any]:
    """返回一段 trace 元数据（session_id/user_id），供跨进程 trace 关联与面板过滤。"""
    return {
        "session_id": session_id,
        "user_id": user_id,
    }


def new_trace_id() -> str:
    """生成一个标准格式的 trace_id（UUID hex），供跨进程透传。

    CallbackHandler 默认自动生成 trace_id；此函数供需要在独立进程（如 RAG MCP）
    里显式开启子 trace 或关联父 trace 时使用。
    """
    return uuid.uuid4().hex


__all__ = [
    "get_langfuse",
    "is_enabled",
    "langfuse_context",
    "langfuse_trace_metadata",
    "make_callbacks",
    "new_trace_id",
]
