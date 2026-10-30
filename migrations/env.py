"""Alembic 环境：只管业务表，LangGraph 自管表一律排除在迁移之外。"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.models import LANGGRAPH_MANAGED_TABLES, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj: Any, name: str, type_: str, reflected: bool, compare_to: Any) -> bool:
    """跳过 LangGraph 通过 .setup() 创建的表，否则 autogenerate 会生成 drop。"""
    return type_ != "table" or name not in LANGGRAPH_MANAGED_TABLES


def _configure(*, connection: Connection | None = None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    _configure(url=get_settings().sqlalchemy_dsn)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_settings().sqlalchemy_dsn, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
