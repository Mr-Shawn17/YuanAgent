"""messages 表新增 execution_graph 列

Revision ID: 0002_add_execution_graph
Revises: 0001_business_tables
Create Date: 2026-10-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_execution_graph"
down_revision: str | Sequence[str] | None = "0001_business_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("execution_graph", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "execution_graph")
