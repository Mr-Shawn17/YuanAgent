from __future__ import annotations

from typing import Any


class AppError(Exception):
    code: str = "app_error"
    status_code: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return {"error": payload}

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class ConfigError(AppError):
    code = "config_error"
    status_code = 500


class InputValidationError(AppError):
    code = "validation_error"
    status_code = 422


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404


class PermissionDeniedError(AppError):
    code = "permission_denied"
    status_code = 403


class UnauthorizedError(AppError):
    code = "unauthorized"
    status_code = 401


class LLMError(AppError):
    code = "llm_error"
    status_code = 502


class LLMTimeoutError(LLMError):
    code = "llm_timeout"
    status_code = 504


class ToolExecutionError(AppError):
    code = "tool_error"
    status_code = 500


class IngestError(AppError):
    code = "ingest_error"
    status_code = 500


class MCPError(AppError):
    code = "mcp_error"
    status_code = 502
