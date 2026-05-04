"""Upgrade event date columns from DATE to TIMESTAMP

Four business-event columns need timestamp precision for deterministic
ordering across both EBS and new_erp schemas: creation_date,
transaction_date, invoice_date, check_date.

Revision ID: 0015_event_dates_to_timestamp
Revises: 0014_sess_recap_chat_idx
Create Date: 2026-05-03
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_event_dates_to_timestamp"
down_revision: str | None = "0014_sess_recap_chat_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = [
    ("po_headers", "creation_date"),
    ("rcv_transactions", "transaction_date"),
    ("ap_invoices", "invoice_date"),
    ("ap_checks", "check_date"),
    ("new_po_headers", "creation_date"),
    ("new_rcv_transactions", "transaction_date"),
    ("new_ap_invoices", "invoice_date"),
    ("new_ap_checks", "check_date"),
]


def _table_exists(name: str) -> bool:
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
    for table, column in _COLUMNS:
        if _table_exists(table):
            op.alter_column(
                table,
                column,
                type_=sa.DateTime(),
                existing_type=sa.Date(),
                existing_nullable=False,
                postgresql_using=f"{column}::timestamp",
            )


def downgrade() -> None:
    for table, column in _COLUMNS:
        if _table_exists(table):
            op.alter_column(
                table,
                column,
                type_=sa.Date(),
                existing_type=sa.DateTime(),
                existing_nullable=False,
                postgresql_using=f"{column}::date",
            )
