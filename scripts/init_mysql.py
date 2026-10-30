"""MySQL 初始化脚本：幂等建库 + LangGraph checkpoint/store 自管表。

用法（Makefile 已封装）：
    make db-init
    # 或
    python scripts/init_mysql.py [--reset-langgraph]

说明：
- 业务表（sessions/messages/audits 等）由 Alembic 迁移管理，不在本脚本职责内。
- LangGraph 的 6 张自管表（checkpoint_migrations/checkpoints/checkpoint_blobs/
  checkpoint_writes/store/store_migrations）由官方 setup() 建表，禁止纳入 Alembic。
- 建库统一使用 utf8mb4 的「字符集默认 collation」（MySQL 8 为 utf8mb4_0900_ai_ci），
  原因见 charset_default_collation() 的注释；collation 不匹配的自管表会被自动删除重建。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import asyncmy  # noqa: E402
from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver  # noqa: E402
from langgraph.store.mysql.asyncmy import AsyncMyStore  # noqa: E402

from app.core.config import Settings, get_settings  # noqa: E402

_VALID_IDENTIFIER = re.compile(r"^\w+$")
# LangGraph 自管表（只存会话快照/长期记忆），重建即清空历史会话
LANGGRAPH_TABLES: tuple[str, ...] = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "store",
    "store_migrations",
)


async def _connect(settings: Settings, *, database: str | None = None) -> asyncmy.Connection:
    return await asyncmy.connect(db=database, **settings.mysql_connect_kwargs)


async def charset_default_collation(settings: Settings) -> str:
    """取 utf8mb4 的服务端默认排序规则（MySQL 8 为 utf8mb4_0900_ai_ci）。

    建库必须用「字符集默认 collation」而不是 utf8mb4_unicode_ci：LangGraph 的
    查询里有 json_table(... columns (channel VARCHAR(150) CHARACTER SET utf8mb4 ...))，
    该生成列的 collation 取字符集默认值，与库/表设置无关；一旦表列是
    utf8mb4_unicode_ci，join 时就报 Illegal mix of collations。
    """
    conn = await _connect(settings)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT DEFAULT_COLLATE_NAME FROM information_schema.CHARACTER_SETS "
                "WHERE CHARACTER_SET_NAME = 'utf8mb4'"
            )
            row = await cursor.fetchone()
    finally:
        conn.close()
    collation = str(row[0]) if row and row[0] else ""
    if not collation or not _VALID_IDENTIFIER.match(collation):
        msg = f"服务端未给出可用的 utf8mb4 默认排序规则：{collation or '(空)'}"
        raise RuntimeError(msg)
    return collation


async def ensure_database(settings: Settings, collation: str) -> None:
    """幂等建库（utf8mb4 + 字符集默认 collation），并把已存在库的默认 collation 对齐。"""
    if not _VALID_IDENTIFIER.match(settings.mysql_database):
        msg = f"MYSQL_DATABASE 含非法字符：{settings.mysql_database}"
        raise ValueError(msg)
    conn = await _connect(settings)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.mysql_database}` "
                f"CHARACTER SET utf8mb4 COLLATE `{collation}`"
            )
            await cursor.execute(
                "SELECT DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
                "WHERE SCHEMA_NAME = %s",
                (settings.mysql_database,),
            )
            row = await cursor.fetchone()
            current = str(row[0]) if row and row[0] else ""
            if current and current != collation:
                await cursor.execute(
                    f"ALTER DATABASE `{settings.mysql_database}` "
                    f"CHARACTER SET utf8mb4 COLLATE `{collation}`"
                )
                print(f"⚠ 库默认 collation 已由 {current} 对齐为 {collation}（已建表不会自动改）")
    finally:
        conn.close()


async def setup_langgraph_tables(settings: Settings) -> None:
    """建 LangGraph checkpoint / store 自管表（官方 setup()，可重复执行）。

    用显式连接而非 from_conn_string：后者不做密码百分号解码，含特殊字符会认证失败。
    """
    conn = await _connect(settings, database=settings.mysql_database)
    try:
        await AsyncMySaver(conn).setup()
        await AsyncMyStore(conn).setup()
    finally:
        conn.close()


async def mismatched_langgraph_tables(settings: Settings, collation: str) -> list[str]:
    """找出 collation 与目标不一致的 LangGraph 自管表（业务表不在检查范围）。"""
    conn = await _connect(settings, database=settings.mysql_database)
    try:
        async with conn.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(LANGGRAPH_TABLES))
            await cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_COLLATION <> %s "
                f"AND TABLE_NAME IN ({placeholders})",
                (settings.mysql_database, collation, *LANGGRAPH_TABLES),
            )
            return sorted(str(row[0]) for row in await cursor.fetchall())
    finally:
        conn.close()


async def drop_langgraph_tables(settings: Settings) -> None:
    """删除 LangGraph 自管表，随后由 setup() 按当前库 collation 重建。"""
    conn = await _connect(settings, database=settings.mysql_database)
    try:
        async with conn.cursor() as cursor:
            for table in LANGGRAPH_TABLES:
                await cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
    finally:
        conn.close()


async def list_tables(settings: Settings) -> list[str]:
    conn = await _connect(settings, database=settings.mysql_database)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("SHOW TABLES")
            rows = await cursor.fetchall()
            return [str(row[0]) for row in rows]
    finally:
        conn.close()


async def _run(reset_langgraph: bool = False) -> None:
    settings = get_settings()
    print(
        f"目标 MySQL：{settings.mysql_user}@{settings.mysql_host}:{settings.mysql_port} "
        f"→ 库 `{settings.mysql_database}`"
    )
    collation = await charset_default_collation(settings)
    await ensure_database(settings, collation)
    print(f"✓ 数据库已就绪（utf8mb4 / {collation}）")

    mismatched = await mismatched_langgraph_tables(settings, collation)
    if mismatched:
        print(f"⚠ 检测到 collation 不匹配的自管表：{', '.join(mismatched)}，将删除重建")
    if reset_langgraph or mismatched:
        if reset_langgraph and not mismatched:
            print("--reset-langgraph 已指定，删除全部 LangGraph 自管表后重建")
        await drop_langgraph_tables(settings)

    await setup_langgraph_tables(settings)
    print("✓ LangGraph checkpoint / store 表已就绪")
    tables = await list_tables(settings)
    print(f"当前共 {len(tables)} 张表：{', '.join(tables) or '(空)'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 MySQL：建库 + LangGraph 自管表")
    parser.add_argument(
        "--reset-langgraph",
        action="store_true",
        help="先删除 6 张 LangGraph 自管表再重建（会清空历史会话与长期记忆）",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(reset_langgraph=args.reset_langgraph))
    except Exception as error:
        print(f"✗ 初始化失败：{type(error).__name__}: {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
