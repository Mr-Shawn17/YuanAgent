"""Web 端轻量 API Key 鉴权：校验 X-API-Key 请求头。

设计：全局 HTTP 中间件（health 豁免），而非 per-route 依赖——所有现有与
未来路由自动受保护，避免遗漏。密钥来自 settings.api_key（.env 的 API_KEY），
留空则鉴权整体关闭（个人工具/本地开发场景，保持向后兼容）。

用常量时间比较（hmac.compare_digest）避免时序侧信道。
"""

from __future__ import annotations

import hmac

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import get_settings

API_KEY_HEADER = "X-API-Key"
# 无需鉴权的路径前缀（健康检查等）
_PUBLIC_PATHS: tuple[str, ...] = ("/health",)


def _verify(key: str, expected: str) -> bool:
    """常量时间比较；expected 为空视为未配置鉴权。"""
    if not expected:
        return True
    return hmac.compare_digest(key.encode("utf-8"), expected.encode("utf-8"))


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """校验 X-API-Key 头，缺失或不匹配返回 401，统一错误体。"""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path.startswith(_PUBLIC_PATHS):
            return await call_next(request)

        settings = get_settings()
        expected = settings.api_key.get_secret_value().strip()
        if not expected:
            # 未配置 API_KEY：鉴权关闭，放行
            return await call_next(request)

        provided = request.headers.get(API_KEY_HEADER, "").strip()
        if not _verify(provided, expected):
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "unauthorized", "message": "缺少或错误的 API Key"}},
            )
        return await call_next(request)


__all__ = ["API_KEY_HEADER", "ApiKeyAuthMiddleware"]
