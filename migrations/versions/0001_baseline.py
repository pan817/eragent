"""baseline: 当前所有表 + 索引

将既有 schema 作为 alembic 起点。对已有数据库执行 ``alembic stamp 0001_baseline``
即可标记为已应用，无需真正建表；对全新数据库执行 ``alembic upgrade head``
会通过 ``metadata.create_all`` 创建全部表。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-04-10
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """全新数据库：基于 metadata 创建全部表 + 长期记忆专用索引。"""
    bind = op.get_bind()

    # 业务 + 可观测性 表
    from core.database.models import Base  # noqa: E402
    import core.observability.tables  # noqa: E402,F401

    Base.metadata.create_all(bind, checkfirst=True)

    # 长期记忆 memories / reports
    from core.memory.tables import metadata_obj as memory_metadata  # noqa: E402

    memory_metadata.create_all(bind, checkfirst=True)

    # PostgreSQL 专用索引：FTS + 复合 btree（SQLite 跳过）
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS memories_content_fts "
            "ON memories USING GIN (to_tsvector('simple', content))"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS memories_user_created "
            "ON memories (user_id, created_at DESC)"
        )


def downgrade() -> None:
    """baseline 不支持 downgrade（会清空所有表，过于危险）。"""
    raise NotImplementedError("baseline migration is not reversible")
