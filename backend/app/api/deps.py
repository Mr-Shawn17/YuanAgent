"""FastAPI 依赖注入：从 app.state 取进程级运行时容器。"""

from __future__ import annotations

from typing import cast

from fastapi import Request

from app.api.runtime import AgentRuntime


def get_runtime(request: Request) -> AgentRuntime:
    """所有路由共享的 AgentRuntime（lifespan 里装配，见 api/main.py）。"""
    return cast(AgentRuntime, request.app.state.runtime)


__all__ = ["get_runtime"]
