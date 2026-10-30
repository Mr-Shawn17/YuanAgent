"""HTTP 服务入口：`uvicorn app.main:app`（Makefile dev 目标绑定此模块）。"""

from app.api.main import app

__all__ = ["app"]
