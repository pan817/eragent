"""trace_runs / trace_spans: 添加复合索引优化查询性能

trace_runs: (session_id, started_at DESC) / (user_id, started_at DESC)
trace_spans: (trace_id, span_type) 加速 token_summary 批量查询

Revision ID: 0007_trace_composite_indexes
Revises: 0006_chat_sessions
Create Date: 2026-04-13
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_trace_composite_indexes"
down_revision: str | None = "0006_chat_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trace_runs_session_started "
        "ON trace_runs (session_id, started_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trace_runs_user_started "
        "ON trace_runs (user_id, started_at DESC)"
    )
    op.create_index(
        "ix_trace_spans_trace_type",
        "trace_spans",
        ["trace_id", "span_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_trace_spans_trace_type", table_name="trace_spans")
    op.drop_index("ix_trace_runs_user_started", table_name="trace_runs")
    op.drop_index("ix_trace_runs_session_started", table_name="trace_runs")
