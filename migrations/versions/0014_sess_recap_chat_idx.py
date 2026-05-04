"""Add session_summaries and chat_index_dead_letter tables for memory v2

Revision ID: 0014_sess_recap_chat_idx
Revises: 0013_etl_sync_state
Create Date: 2026-05-01
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON

revision: str = "0014_sess_recap_chat_idx"
down_revision: str | None = "0013_etl_sync_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(name: str) -> bool:
    """Check if a table already exists in the database."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT FROM information_schema.tables"
            "  WHERE table_name = :name"
            ")"
        ),
        {"name": name},
    )
    return result.scalar() or False


def upgrade() -> None:
    # -- session_summaries (SESSION_RECAP structured mirror) --
    if not _table_exists("session_summaries"):
        op.create_table(
            "session_summaries",
            sa.Column("session_id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("summary_text", sa.Text, nullable=False),
            sa.Column("key_entities", JSON, nullable=False, server_default=sa.text("'{}'")),
            sa.Column("tags", JSON, nullable=False, server_default=sa.text("'[]'")),
            sa.Column("analysis_count", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
            sa.Column("memory_id", sa.String(36), nullable=True),
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
        op.create_index(
            "session_summaries_user",
            "session_summaries",
            ["user_id", sa.text("updated_at DESC")],
        )
        op.create_index(
            "session_summaries_tags",
            "session_summaries",
            ["tags"],
            postgresql_using="gin",
        )

    # -- chat_index_dead_letter (vector index retry queue) --
    if not _table_exists("chat_index_dead_letter"):
        op.create_table(
            "chat_index_dead_letter",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("fragment_data", JSON, nullable=False),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("retry_count", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("last_retry_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("chat_index_dead_letter")
    op.drop_index("session_summaries_tags", table_name="session_summaries")
    op.drop_index("session_summaries_user", table_name="session_summaries")
    op.drop_table("session_summaries")
