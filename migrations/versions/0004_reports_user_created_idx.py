"""reports: 新增 (user_id, created_at DESC) 复合索引

list_reports 的查询形态为：
    WHERE user_id = ? ORDER BY created_at DESC LIMIT ?
原有单列 user_id 索引无法覆盖排序，PG 需要额外 filesort。
新增复合索引后可直接 index scan，按顺序取前 N 行，无需排序步骤。

Revision ID: 0004_reports_user_created_idx
Revises: 0003_memories_rename_metadata_to_attrs
Create Date: 2026-04-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_reports_user_created_idx"
down_revision: str | None = "0003_memories_rename_metadata_to_attrs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "reports_user_created",
        "reports",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("reports_user_created", table_name="reports")
