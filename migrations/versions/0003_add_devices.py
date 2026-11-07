"""devices 表：本地执行器设备注册

Revision ID: 0003_add_devices
Revises: 0002_add_execution_graph
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_add_devices"
down_revision: str | Sequence[str] | None = "0002_add_execution_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_table("devices")
