"""memories: 增加 content_hash 列与去重索引

用于写入端的近似重复识别（问题 4）。

Revision ID: 0002_memories_content_hash
Revises: 0001_baseline
Create Date: 2026-04-10
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_memories_content_hash"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column("content_hash", sa.String(length=16), nullable=True),
    )
    op.create_index(
        "memories_user_hash_created",
        "memories",
        ["user_id", "content_hash", sa.text("created_at DESC")],
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # 老数据回填：与 Python 端 sha256(normalize(content))[:16] 不同，
        # 这里仅用 md5(lower+collapse_ws) 兜底，保证老数据彼此可去重；
        # 老数据与新写入的 hash 不会发生冲突（不同算法），可接受。
        op.execute(
            "UPDATE memories "
            "SET content_hash = SUBSTRING("
            "  MD5(LOWER(REGEXP_REPLACE(content, '\\s+', ' ', 'g'))), 1, 16"
            ") "
            "WHERE content_hash IS NULL"
        )


def downgrade() -> None:
    op.drop_index("memories_user_hash_created", table_name="memories")
    op.drop_column("memories", "content_hash")
