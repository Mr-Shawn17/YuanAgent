from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

# MySQL 默认 wait_timeout 8 小时；主动回收连接避免 "server has gone away"
POOL_RECYCLE_SECONDS = 1800


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """按配置创建业务表的 async engine（DSN 已在 Settings 里做好百分号转义）。"""
    resolved = settings or get_settings()
    return create_async_engine(
        resolved.sqlalchemy_dsn,
        pool_size=resolved.mysql_pool_size,
        max_overflow=resolved.mysql_max_overflow,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_pre_ping=True,
        echo=resolved.mysql_echo,
    )


class Database:
    """engine + sessionmaker 的轻量封装。

    约定：repo 内部只 flush、不 commit，事务边界统一收在 session() 上下文里，
    这样一次对话轮次的多条写入要么全成功要么全回滚。
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def aclose(self) -> None:
        await self.engine.dispose()


def build_database(settings: Settings | None = None) -> Database:
    return Database(create_engine(settings))


@asynccontextmanager
async def database_scope(settings: Settings | None = None) -> AsyncIterator[Database]:
    """一次性使用场景（脚本、单次请求）的便捷上下文。"""
    database = build_database(settings)
    try:
        yield database
    finally:
        await database.aclose()


__all__ = ["POOL_RECYCLE_SECONDS", "Database", "build_database", "create_engine", "database_scope"]
