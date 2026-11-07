"""单实例锁：同一台电脑同时只允许一个 runner 进程。

Windows 用 msvcrt.locking 锁文件；POSIX 用 fcntl.flock。
获取失败抛 SingleInstanceError（入口捕获后直接退出新进程）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

from runner.config import app_dir

LOCK_NAME = "runner.lock"


class SingleInstanceError(RuntimeError):
    """已有实例在运行。"""


class SingleInstanceLock:
    """进程存活期内持有锁文件；close() 释放（进程退出时 OS 也会回收）。"""

    def __init__(self, lock_path: Path | None = None) -> None:
        self._path = lock_path or (app_dir() / LOCK_NAME)
        self._handle: IO[bytes] | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                handle.seek(0)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise SingleInstanceError(
                f"YuanRunner 已在运行（锁文件：{self._path}），本次启动退出"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode())
        handle.flush()
        self._handle = handle

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


__all__ = ["LOCK_NAME", "SingleInstanceError", "SingleInstanceLock"]
