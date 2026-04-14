"""reports: add trace_id column + UNIQUE index

Rationale: /analyze/tasks/{trace_id} snapshot falls back to DB when the
registry entry is not on the current worker (multi-worker deployment).
Previously reports were keyed only by report_id, so the fallback had no way
to retrieve the full AnalysisResult by trace_id and returned result=null —
frontend would see "status=ok, result=null" and render "analysis failed".

This migration adds a nullable trace_id column with a UNIQUE index so the
snapshot loader can SELECT reports WHERE trace_id=?. UNIQUE expresses the
business invariant (one analyze → one report); PG allows multiple NULLs so
historical rows with trace_id=NULL remain valid.

Revision ID: 0009_reports_trace_id
Revises: 0008_timezone_normalization
Create Date: 2026-04-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_reports_trace_id"
down_revision: str | None = "0008_timezone_normalization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("trace_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "reports_trace_id",
        "reports",
        ["trace_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("reports_trace_id", table_name="reports")
    op.drop_column("reports", "trace_id")
