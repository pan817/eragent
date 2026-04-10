"""memories: 将 metadata 列重命名为 attrs

``metadata`` 是 SQLAlchemy Declarative ORM 的保留属性名，
与列名冲突会导致未来迁移 ORM 时无法正常定义 model。
重命名为 ``attrs`` 消除反模式，纯元数据操作，无需重写数据。

Revision ID: 0003_memories_rename_metadata_to_attrs
Revises: 0002_memories_content_hash
Create Date: 2026-04-10
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_memories_rename_metadata_to_attrs"
down_revision: str | None = "0002_memories_content_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # PostgreSQL: 纯元数据操作，瞬间完成，不锁表
        op.alter_column("memories", "metadata", new_column_name="attrs")
    else:
        # SQLite 3.25+ 支持 RENAME COLUMN，用 batch 模式兼容旧版
        with op.batch_alter_table("memories") as batch_op:
            batch_op.alter_column("metadata", new_column_name="attrs")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column("memories", "attrs", new_column_name="metadata")
    else:
        with op.batch_alter_table("memories") as batch_op:
            batch_op.alter_column("attrs", new_column_name="metadata")
