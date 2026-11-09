"""FastAPI 应用工厂 + lifespan 装配。

lifespan 一次性组装进程级共享资源（对齐 cli.py _chat_loop 的装配顺序）：
配置硬校验 → MySQL database/checkpointer/store → MCP → 双档模型 → AgentRuntime。
_build_runtime 单独拆出，测试用 monkeypatch 替换成 InMemory 版本。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.agents.builder import mysql_checkpointer, mysql_store
from app.api.auth import ApiKeyAuthMiddleware
from app.api.routes_chat import router as chat_router
from app.api.routes_openai import router as openai_router
from app.api.routes_runner import router as runner_router
from app.api.routes_sessions import router as sessions_router
from app.api.runtime import AgentRuntime
from app.core.config import get_agent_config, get_settings
from app.core.errors import AppError, ConfigError
from app.core.logging import get_logger, setup_logging
from app.db.engine import build_database
from app.llm.factory import get_chat_model
from app.mcp import McpManager

logger = get_logger(__name__)


async def _build_runtime() -> tuple[AgentRuntime, AsyncExitStack]:
    """组装进程级共享资源；任何一步失败都要完整回收，不留半开连接。"""
    settings = get_settings()
    settings.ensure_dirs()
    if not settings.profile_for("main").is_configured:
        raise ConfigError("LLM 主模型未配置（LLM_API_KEY / DASHSCOPE_API_KEY 均为空），拒绝启动")
    agent_config = get_agent_config()
    database = build_database(settings)
    stack = AsyncExitStack()
    try:
        checkpointer = await stack.enter_async_context(mysql_checkpointer(settings))
        store = await stack.enter_async_context(mysql_store(settings))
        mcp_manager = await stack.enter_async_context(McpManager())
        model = get_chat_model("main", settings=settings)
        fast_model = get_chat_model("fast", settings=settings, streaming=False)
    except BaseException:
        await stack.aclose()
        await database.aclose()
        raise
    runtime = AgentRuntime(
        settings=settings,
        agent_config=agent_config,
        checkpointer=checkpointer,
        store=store,
        model=model,
        fast_model=fast_model,
        database=database,
        mcp_tools=list(mcp_manager.tools),
        mcp_tiers=dict(mcp_manager.tiers),
    )
    if mcp_manager.failed:
        logger.warning("mcp_partial_failed", failed=sorted(mcp_manager.failed))
    return runtime, stack


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(
        settings.log_level,
        json_output=settings.log_json,
        log_dir=settings.log_dir_path,
        retention_days=settings.log_retention_days,
    )
    runtime, stack = await _build_runtime()
    # 绑定服务主循环：worker 线程里的同步工具经 hub.dispatch_threadsafe 投递回来
    # （避免跨事件循环唤醒不可靠导致的概率性挂起，见 local_hub.dispatch_threadsafe）
    runtime.local_hub.bind_loop(asyncio.get_running_loop())
    app.state.runtime = runtime
    logger.info("api_runtime_ready", user_id=runtime.user_id)
    try:
        yield
    finally:
        await runtime.shutdown()
        await stack.aclose()
        if runtime.database is not None:
            await runtime.database.aclose()
        logger.info("api_runtime_closed")


def create_app() -> FastAPI:
    """应用工厂：CORS + 统一错误格式 + 路由挂载。"""
    settings = get_settings()
    application = FastAPI(
        title="日常聊天 Agent API",
        version=__version__,
        lifespan=_lifespan,
    )
    # 轻量 API Key 鉴权（health 豁免）；API_KEY 留空时整体关闭
    application.add_middleware(ApiKeyAuthMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @application.exception_handler(RequestValidationError)
    async def _handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "请求体校验失败",
                    "details": {"errors": jsonable_encoder(exc.errors())},
                }
            },
        )

    application.include_router(chat_router)
    application.include_router(sessions_router)
    application.include_router(openai_router)
    application.include_router(runner_router)

    @application.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    # 说明：/ws/runner 是 WebSocket 端点，走 ASGI 层，天然不经过
    # ApiKeyAuthMiddleware（BaseHTTPMiddleware 只拦 HTTP）；它用设备令牌自行鉴权。

    return application


app = create_app()

__all__ = ["app", "create_app"]
