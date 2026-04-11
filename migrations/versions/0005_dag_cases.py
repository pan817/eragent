"""dag_cases: DAG 案例持久化表

存储成功执行的 DAG 案例，PostgreSQL 为权威数据源，
服务启动时从 PG 全量加载到 Chroma 检索缓存。

Revision ID: 0005_dag_cases
Revises: 0004_reports_user_created_idx
Create Date: 2026-04-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_dag_cases"
down_revision: str | None = "0004_reports_user_created_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dag_cases",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("query_hash", sa.String(16), unique=True, nullable=False),
        sa.Column("query", sa.Text, nullable=False),
        sa.Column("analysis_type", sa.String(64), nullable=False),
        sa.Column(
            "dag_definition",
            sa.dialects.postgresql.JSONB,
            nullable=False,
        ),
        sa.Column("route_type", sa.String(32), nullable=False),
        sa.Column("task_count", sa.Integer, nullable=False),
        sa.Column("duration_sec", sa.Float, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("dag_cases_analysis_type", "dag_cases", ["analysis_type"])


def downgrade() -> None:
    op.drop_index("dag_cases_analysis_type", table_name="dag_cases")
    op.drop_table("dag_cases")
