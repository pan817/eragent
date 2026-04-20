"""EBS table expansion: rename columns, add new columns, add 13 new tables

Drop and recreate all P2P business tables to align with Oracle EBS naming:
- Column renames: supplier_id->vendor_id, supplier_name->vendor_name,
  item_code->item_id, category->category_id, payment_terms->terms_id,
  status->enabled_flag(ApSupplier)/approval_status(ApInvoice),
  invoice_number->invoice_num, payment_id->check_id, etc.
- Table rename: ap_payments -> ap_checks
- New EBS columns on all 7 existing tables
- 13 new tables: po_distributions, rcv_shipment_headers, rcv_shipment_lines,
  ap_invoice_lines, ap_invoice_distributions, ap_invoice_payments,
  ap_payment_schedules, ap_supplier_sites, mtl_system_items,
  pon_auction_headers, pon_bid_headers, okc_k_headers, okc_k_lines

NOTE: This migration drops all P2P business data. Run reset_and_seed after upgrade.

Revision ID: 0012_ebs_table_expansion
Revises: 0011_memory_type_system
Create Date: 2026-04-20
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_ebs_table_expansion"
down_revision: str | None = "0011_memory_type_system"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Old tables to drop (child-first order for FK safety)
_OLD_TABLES = [
    "ap_payments",
    "ap_invoices",
    "rcv_transactions",
    "po_line_locations",
    "po_lines",
    "po_headers",
    "ap_suppliers",
]

# All P2P tables in the new schema (parent-first for creation)
_NEW_TABLES = [
    "ap_suppliers",
    "po_headers",
    "po_lines",
    "po_line_locations",
    "rcv_transactions",
    "ap_invoices",
    "ap_checks",
    "po_distributions",
    "rcv_shipment_headers",
    "rcv_shipment_lines",
    "ap_invoice_lines",
    "ap_invoice_distributions",
    "ap_invoice_payments",
    "ap_payment_schedules",
    "ap_supplier_sites",
    "mtl_system_items",
    "pon_auction_headers",
    "pon_bid_headers",
    "okc_k_headers",
    "okc_k_lines",
]


def upgrade() -> None:
    # -- 1. Drop old P2P tables --
    for tbl in _OLD_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")

    # -- 2. Create all tables from current ORM models --
    from core.database.models import Base

    bind = op.get_bind()
    tables_to_create = [
        Base.metadata.tables[name]
        for name in _NEW_TABLES
        if name in Base.metadata.tables
    ]
    Base.metadata.create_all(bind, tables=tables_to_create)


def downgrade() -> None:
    # Drop all new tables (child-first)
    drop_order = list(reversed(_NEW_TABLES))
    for tbl in drop_order:
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
