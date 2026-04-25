"""New ERP ORM models — placeholder with different table names, same column layout.

Shares ``core.database.models.Base`` so tables are visible to the same engine.
Foreign keys reference ``new_*`` tables (self-contained, no cross-schema FK).
Relationships are omitted — the repository uses explicit JOINs only.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from core.database.models import Base


class NewApSupplier(Base):
    __tablename__ = "new_ap_suppliers"

    vendor_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    vendor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    supplier_site_id: Mapped[str] = mapped_column(String(20), nullable=False)
    terms_id: Mapped[str] = mapped_column(String(20), nullable=False)
    enabled_flag: Mapped[str] = mapped_column(String(20), nullable=False, default="Y")
    segment1: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    vendor_type_lookup_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    start_date_active: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end_date_active: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class NewPoHeader(Base):
    __tablename__ = "new_po_headers"

    po_header_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    vendor_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("new_ap_suppliers.vendor_id"), nullable=False,
    )
    vendor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="APPROVED")
    creation_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class NewPoLine(Base):
    __tablename__ = "new_po_lines"

    po_line_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_header_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("new_po_headers.po_header_id"), nullable=False,
    )
    po_number: Mapped[str] = mapped_column(String(30), nullable=False)
    line_num: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    item_id: Mapped[str] = mapped_column(String(20), nullable=False)
    item_description: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    category_id: Mapped[str] = mapped_column(String(30), nullable=False)
    standard_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class NewPoLineLocation(Base):
    __tablename__ = "new_po_line_locations"

    line_location_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_line_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("new_po_lines.po_line_id"), nullable=False,
    )
    po_number: Mapped[str] = mapped_column(String(30), nullable=False)
    promised_date: Mapped[date] = mapped_column(Date, nullable=False)
    need_by_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class NewRcvTransaction(Base):
    __tablename__ = "new_rcv_transactions"

    transaction_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shipment_header_id: Mapped[int] = mapped_column(Integer, nullable=False)
    po_number: Mapped[str] = mapped_column(String(30), nullable=False)
    po_line_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("new_po_lines.po_line_id"), nullable=False,
    )
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False, default="RECEIVE")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    vendor_id: Mapped[str] = mapped_column(String(20), nullable=False)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class NewApInvoice(Base):
    __tablename__ = "new_ap_invoices"

    invoice_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_num: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    po_number: Mapped[str] = mapped_column(String(30), nullable=False)
    vendor_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("new_ap_suppliers.vendor_id"), nullable=False,
    )
    vendor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    invoice_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    discount_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    approval_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="VALIDATED",
    )
    terms_id: Mapped[str] = mapped_column(String(20), nullable=False, default="NET30")
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class NewApPayment(Base):
    __tablename__ = "new_ap_checks"

    check_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    check_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    invoice_num: Mapped[str] = mapped_column(String(30), nullable=False)
    vendor_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("new_ap_suppliers.vendor_id"), nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    check_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_method_code: Mapped[str] = mapped_column(
        String(20), nullable=False, default="BANK_TRANSFER",
    )
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
