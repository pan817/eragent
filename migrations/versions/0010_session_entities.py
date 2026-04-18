"""session_entities: structured entity context per session

Store business entities (po_number, supplier_id, etc.) as a JSON dict
per session, replacing the fragile "regex from last message text" approach.
Enables reliable multi-turn entity reference resolution.

Revision ID: 0010_session_entities
Revises: 0009_reports_trace_id
Create Date: 2026-04-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_session_entities"
down_revision: str | None = "0009_reports_trace_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_entities",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column(
            "entities",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("session_entities")
