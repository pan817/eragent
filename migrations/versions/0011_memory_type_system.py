"""memory type system: add columns, indexes, and consolidation log table

Add structured memory type support:
- memories table: entity_id, expires_at, consolidated_at, is_consolidated, source_ids
- 4 new indexes for type-aware queries, entity lookup, TTL purge, consolidation
- memory_consolidation_log table for consolidation state tracking

Revision ID: 0011_memory_type_system
Revises: 0010_session_entities
Create Date: 2026-04-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_memory_type_system"
down_revision: str | None = "0010_session_entities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table: str, column: str) -> bool:
    """Check if a column exists (idempotent migration support)."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    return any(c["name"] == column for c in insp.get_columns(table))


def _table_exists(table: str) -> bool:
    """Check if a table exists (idempotent migration support)."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    return table in insp.get_table_names()


def _index_exists(index_name: str) -> bool:
    """Check if an index exists on any table."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": index_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    # -- memories table: new columns (idempotent) --
    if not _column_exists("memories", "entity_id"):
        op.add_column("memories", sa.Column("entity_id", sa.String(128), nullable=True))
    if not _column_exists("memories", "expires_at"):
        op.add_column(
            "memories",
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _column_exists("memories", "consolidated_at"):
        op.add_column(
            "memories",
            sa.Column("consolidated_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _column_exists("memories", "is_consolidated"):
        op.add_column(
            "memories",
            sa.Column(
                "is_consolidated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    if not _column_exists("memories", "source_ids"):
        op.add_column("memories", sa.Column("source_ids", sa.JSON(), nullable=True))

    # -- memories table: new indexes (idempotent) --
    if not _index_exists("memories_type_user"):
        op.create_index(
            "memories_type_user",
            "memories",
            ["memory_type", "user_id"],
        )
    if not _index_exists("memories_entity"):
        op.create_index(
            "memories_entity",
            "memories",
            ["user_id", "entity_id"],
            postgresql_where=sa.text("entity_id IS NOT NULL"),
        )
    if not _index_exists("memories_expires"):
        op.create_index(
            "memories_expires",
            "memories",
            ["expires_at"],
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        )
    if not _index_exists("memories_consolidation"):
        op.create_index(
            "memories_consolidation",
            "memories",
            ["user_id", "is_consolidated", "memory_type"],
        )

    # -- memory_consolidation_log table (idempotent) --
    if not _table_exists("memory_consolidation_log"):
        op.create_table(
            "memory_consolidation_log",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column(
                "input_count", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column(
                "merged_count", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column(
                "pruned_count", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column(
                "llm_used",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
        )
    if not _index_exists("consolidation_user_status"):
        op.create_index(
            "consolidation_user_status",
            "memory_consolidation_log",
            ["user_id", "status"],
        )
    if not _index_exists("consolidation_completed"):
        op.create_index(
            "consolidation_completed",
            "memory_consolidation_log",
            ["user_id", sa.text("completed_at DESC")],
        )


def downgrade() -> None:
    op.drop_table("memory_consolidation_log")
    op.drop_index("memories_consolidation", table_name="memories")
    op.drop_index("memories_expires", table_name="memories")
    op.drop_index("memories_entity", table_name="memories")
    op.drop_index("memories_type_user", table_name="memories")
    op.drop_column("memories", "source_ids")
    op.drop_column("memories", "is_consolidated")
    op.drop_column("memories", "consolidated_at")
    op.drop_column("memories", "expires_at")
    op.drop_column("memories", "entity_id")
