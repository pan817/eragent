"""Add etl_sync_state table for ETL watermark tracking

Revision ID: 0013_etl_sync_state
Revises: 0012_ebs_table_expansion
Create Date: 2026-04-20
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_etl_sync_state"
down_revision: str | None = "0012_ebs_table_expansion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from core.etl.tables import ETLSyncState
    from core.database.models import Base

    bind = op.get_bind()
    ETLSyncState.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS etl_sync_state CASCADE")
