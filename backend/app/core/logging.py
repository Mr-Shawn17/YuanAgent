from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import structlog

from app.core.config import PROJECT_ROOT

REDACTED = "***"

# 文件日志：默认项目根/log，按天一文件，仅保留近 N 天（启动时清理过期文件）
DEFAULT_LOG_DIR = PROJECT_ROOT / "log"
DEFAULT_RETENTION_DAYS = 7
_LOG_FILE_PREFIX = "backend-"

_SENSITIVE_KEY_HINTS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "password",
    "passwd",
    "secret",
    "token",
    "access_key",
    "private_key",
    "cookie",
    "credential",
)

_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9._\-]+|AKIA[0-9A-Z]{12,})"
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _SENSITIVE_KEY_HINTS)


def _redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub(REDACTED, value)
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and _is_sensitive_key(key)
            else _redact(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth=depth + 1) for item in value]
    return value


def secret_redactor(
    logger: logging.Logger,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key in {"event", "level", "timestamp", "logger", "logger_name", "exc_info"}:
            if key == "event" and isinstance(event_dict[key], str):
                event_dict[key] = _SECRET_VALUE_RE.sub(REDACTED, event_dict[key])
            continue
        if isinstance(key, str) and _is_sensitive_key(key):
            event_dict[key] = REDACTED
            continue
        event_dict[key] = _redact(event_dict[key])
    return event_dict


def cleanup_old_logs(log_dir: Path, prefix: str, retention_days: int) -> None:
    """删除超过保留期的日志文件（按文件名中的日期判断，失败静默不影响启动）。"""
    if retention_days <= 0 or not log_dir.exists():
        return
    cutoff = datetime.now() - timedelta(days=retention_days)
    cutoff_name = f"{prefix}{cutoff.strftime('%Y-%m-%d')}"
    try:
        for file in log_dir.glob(f"{prefix}*.log"):
            if file.name <= cutoff_name:  # 日期字符串可直接字典序比较
                file.unlink(missing_ok=True)
    except OSError:
        pass


def _make_file_handler(log_dir: Path, prefix: str) -> logging.Handler:
    """按天命名的文件 handler：backend-YYYY-MM-DD.log（当天文件，启动跨天不切分）。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    filename = log_dir / f"{prefix}{datetime.now().strftime('%Y-%m-%d')}.log"
    handler = logging.FileHandler(filename, encoding="utf-8")
    # 文件用无色 ConsoleRenderer（颜色码写进文件会污染 grep）
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            secret_redactor,
            structlog.dev.ConsoleRenderer(colors=False, sort_keys=False),
        ],
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
        ],
    )
    handler.setFormatter(formatter)
    return handler


def setup_logging(
    level: str = "INFO",
    *,
    json_output: bool = False,
    force: bool = False,
    log_dir: str | Path | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty(), sort_keys=False)
    )

    # ConsoleRenderer 自己处理 exc_info（且明确要求不要叠加 format_exc_info）；
    # JSONRenderer 不会渲染堆栈，所以只在 JSON 模式下把 exc_info 转成 exception 字符串。
    exc_info_processor: list[structlog.types.Processor] = (
        [structlog.processors.format_exc_info] if json_output else []
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.PositionalArgumentsFormatter(),
            *exc_info_processor,
            secret_redactor,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=not force,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            secret_redactor,
            renderer,
        ],
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    # 文件日志：与终端同一脱敏链路；_make_file_handler 内部 mkdir
    resolved_log_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    cleanup_old_logs(resolved_log_dir, _LOG_FILE_PREFIX, retention_days)
    file_handler = _make_file_handler(resolved_log_dir, _LOG_FILE_PREFIX)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.addHandler(file_handler)
    root.setLevel(log_level)

    for noisy in ("httpx", "httpcore", "openai", "urllib3", "qdrant_client"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))
    # asyncmy 会把 MySQL 的幂等 note（如 "Table ... already exists"）以 WARNING 打出，无害，直接静音
    logging.getLogger("asyncmy").setLevel(max(log_level, logging.ERROR))


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name, **initial_values))


__all__ = [
    "DEFAULT_LOG_DIR",
    "DEFAULT_RETENTION_DAYS",
    "REDACTED",
    "cleanup_old_logs",
    "get_logger",
    "secret_redactor",
    "setup_logging",
]
