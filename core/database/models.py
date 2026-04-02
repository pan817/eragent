"""
SQLAlchemy ORM 模型，对齐 Oracle EBS 表结构。

定义 P2P 流程核心表：供应商、采购订单（头/行/位置）、收货、发票、付款。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """ORM 基类。"""


class ApSupplier(Base):
    """供应商主数据（AP_SUPPLIERS）。"""

    __tablename__ = "ap_suppliers"

    supplier_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    supplier_name: Mapped[str] = mapped_column(String(100), nullable=False)
    supplier_site_id: Mapped[str] = mapped_column(String(20), nullable=False)
    payment_terms: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")

    # 关系
    po_headers: Mapped[list[PoHeader]] = relationship(back_populates="supplier")
    invoices: Mapped[list[ApInvoice]] = relationship(back_populates="supplier")
    payments: Mapped[list[ApPayment]] = relationship(back_populates="supplier")


class PoHeader(Base):
    """采购订单头（PO_HEADERS_ALL）。"""

    __tablename__ = "po_headers"

    po_header_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    supplier_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("ap_suppliers.supplier_id"), nullable=False
    )
    supplier_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="APPROVED")
    creation_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")

    # 关系
    supplier: Mapped[ApSupplier] = relationship(back_populates="po_headers")
    lines: Mapped[list[PoLine]] = relationship(back_populates="header")


class PoLine(Base):
    """采购订单行（PO_LINES_ALL）。"""

    __tablename__ = "po_lines"

    po_line_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_header_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("po_headers.po_header_id"), nullable=False
    )
    po_number: Mapped[str] = mapped_column(String(30), nullable=False)
    line_num: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    item_code: Mapped[str] = mapped_column(String(20), nullable=False)
    item_description: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    standard_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)

    # 关系
    header: Mapped[PoHeader] = relationship(back_populates="lines")
    locations: Mapped[list[PoLineLocation]] = relationship(back_populates="line")
    rcv_transactions: Mapped[list[RcvTransaction]] = relationship(back_populates="po_line")


class PoLineLocation(Base):
    """采购订单行位置（PO_LINE_LOCATIONS_ALL）。"""

    __tablename__ = "po_line_locations"

    line_location_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_line_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("po_lines.po_line_id"), nullable=False
    )
    po_number: Mapped[str] = mapped_column(String(30), nullable=False)
    promised_date: Mapped[date] = mapped_column(Date, nullable=False)
    need_by_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    # 关系
    line: Mapped[PoLine] = relationship(back_populates="locations")


class RcvTransaction(Base):
    """收货事务（RCV_TRANSACTIONS）。"""

    __tablename__ = "rcv_transactions"

    transaction_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shipment_header_id: Mapped[int] = mapped_column(Integer, nullable=False)
    po_number: Mapped[str] = mapped_column(String(30), nullable=False)
    po_line_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("po_lines.po_line_id"), nullable=False
    )
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False, default="RECEIVE")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    supplier_id: Mapped[str] = mapped_column(String(20), nullable=False)

    # 关系
    po_line: Mapped[PoLine] = relationship(back_populates="rcv_transactions")


class ApInvoice(Base):
    """发票（AP_INVOICES_ALL）。"""

    __tablename__ = "ap_invoices"

    invoice_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    po_number: Mapped[str] = mapped_column(String(30), nullable=False)
    supplier_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("ap_suppliers.supplier_id"), nullable=False
    )
    supplier_name: Mapped[str] = mapped_column(String(100), nullable=False)
    invoice_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    discount_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="VALIDATED")
    payment_terms: Mapped[str] = mapped_column(String(20), nullable=False, default="NET30")

    # 关系
    supplier: Mapped[ApSupplier] = relationship(back_populates="invoices")


class ApPayment(Base):
    """付款（AP_PAYMENTS_ALL）。"""

    __tablename__ = "ap_payments"

    payment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    invoice_number: Mapped[str] = mapped_column(String(30), nullable=False)
    supplier_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("ap_suppliers.supplier_id"), nullable=False
    )
    payment_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_method: Mapped[str] = mapped_column(String(20), nullable=False, default="BANK_TRANSFER")

    # 关系
    supplier: Mapped[ApSupplier] = relationship(back_populates="payments")
