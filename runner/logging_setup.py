"""文件日志：%LOCALAPPDATA%\\YuanAgent\\logs\\runner.log（按 5MB 轮转，留 3 份）。

无窗口运行，一切异常排查依赖此日志。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from runner.config import logs_dir

LOG_NAME = "runner.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def _has_yuan_handler(root: logging.Logger) -> bool:
    return any(
        isinstance(h, RotatingFileHandler) and getattr(h, "_yuan_marker", False)
        for h in root.handlers
    )


def setup_logging(*, debug: bool = False) -> logging.Logger:
    """初始化根日志器并返回 runner 主日志器。幂等（重复调用只加一次 handler）。"""
    logs_dir().mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not _has_yuan_handler(root):
        handler = RotatingFileHandler(
            logs_dir() / LOG_NAME,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler._yuan_marker = True  # type: ignore[attr-defined]
        root.addHandler(handler)
        root.setLevel(logging.DEBUG if debug else logging.INFO)
    return logging.getLogger("runner")


__all__ = ["LOG_NAME", "setup_logging"]
