"""trace_runs / trace_spans: started_at / finished_at 升级为 TIMESTAMPTZ

Rationale: 全系统统一使用业务时区（默认 Asia/Shanghai）。
原先这两张表使用的是 TIMESTAMP WITHOUT TIME ZONE,配合 Python 侧
``datetime.utcnow()`` 写入,语义为 naive UTC。升级后改为 TIMESTAMPTZ,
存量数据按 UTC 解释一次（``AT TIME ZONE 'UTC'``),绝对时刻保留不变;
新数据由 ``core.time_utils.now_cn`` 以 +08 带时区写入。

Revision ID: 0008_timezone_normalization
Revises: 0007_trace_composite_indexes
Create Date: 2026-04-14
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_timezone_normalization"
down_revision: str | None = "0007_trace_composite_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite has no timezone concept; skip the type change there.
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        "ALTER TABLE trace_runs "
        "ALTER COLUMN started_at TYPE TIMESTAMPTZ "
        "USING started_at AT TIME ZONE 'UTC', "
        "ALTER COLUMN finished_at TYPE TIMESTAMPTZ "
        "USING finished_at AT TIME ZONE 'UTC'"
    )
    op.execute(
        "ALTER TABLE trace_spans "
        "ALTER COLUMN started_at TYPE TIMESTAMPTZ "
        "USING started_at AT TIME ZONE 'UTC', "
        "ALTER COLUMN finished_at TYPE TIMESTAMPTZ "
        "USING finished_at AT TIME ZONE 'UTC'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        "ALTER TABLE trace_runs "
        "ALTER COLUMN started_at TYPE TIMESTAMP "
        "USING started_at AT TIME ZONE 'UTC', "
        "ALTER COLUMN finished_at TYPE TIMESTAMP "
        "USING finished_at AT TIME ZONE 'UTC'"
    )
    op.execute(
        "ALTER TABLE trace_spans "
        "ALTER COLUMN started_at TYPE TIMESTAMP "
        "USING started_at AT TIME ZONE 'UTC', "
        "ALTER COLUMN finished_at TYPE TIMESTAMP "
        "USING finished_at AT TIME ZONE 'UTC'"
    )
