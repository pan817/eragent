"""
SQLAlchemy ORM 模型，对齐 Oracle EBS 表结构。

定义 P2P 流程核心表：供应商、采购订单（头/行/位置/分配）、收货（头/行/事务）、
发票（头/行/分配/付款关联/付款计划）、付款、主数据（物料/供应商地点）、寻源合同。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """ORM 基类。"""


class ApSupplier(Base):
    """供应商主数据（AP_SUPPLIERS）。"""

    __tablename__ = "ap_suppliers"

    vendor_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    vendor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    supplier_site_id: Mapped[str] = mapped_column(String(20), nullable=False)
    terms_id: Mapped[str] = mapped_column(String(20), nullable=False)
    enabled_flag: Mapped[str] = mapped_column(String(20), nullable=False, default="Y")

    # EBS 扩充字段
    segment1: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    vendor_type_lookup_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    start_date_active: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end_date_active: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    num_1099: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    tax_reporting_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    standard_industry_class: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    minority_group_lookup_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    women_owned_flag: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    small_business_flag: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # 关系
    po_headers: Mapped[list[PoHeader]] = relationship(back_populates="supplier")
    invoices: Mapped[list[ApInvoice]] = relationship(back_populates="supplier")
    payments: Mapped[list[ApPayment]] = relationship(back_populates="supplier")
    sites: Mapped[list[ApSupplierSite]] = relationship(back_populates="supplier")


class PoHeader(Base):
    """采购订单头（PO_HEADERS_ALL）。"""

    __tablename__ = "po_headers"

    po_header_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    vendor_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("ap_suppliers.vendor_id"), nullable=False
    )
    vendor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="APPROVED")
    creation_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")

    # EBS 扩充字段
    type_lookup_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    revision_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    approved_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    authorization_status: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    buyer_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    org_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comments: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    blanket_total_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    rate_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 5), nullable=True)
    agent_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    closed_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # 关系
    supplier: Mapped[ApSupplier] = relationship(back_populates="po_headers")
    lines: Mapped[list[PoLine]] = relationship(back_populates="header")
    distributions: Mapped[list[PoDistribution]] = relationship(back_populates="header")


class PoLine(Base):
    """采购订单行（PO_LINES_ALL）。"""

    __tablename__ = "po_lines"

    po_line_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_header_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("po_headers.po_header_id"), nullable=False
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

    # EBS 扩充字段
    line_type_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    unit_meas_lookup_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    from_header_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    from_line_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    closed_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    contract_num: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    vendor_product_num: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # 关系
    header: Mapped[PoHeader] = relationship(back_populates="lines")
    locations: Mapped[list[PoLineLocation]] = relationship(back_populates="line")
    distributions: Mapped[list[PoDistribution]] = relationship(back_populates="line")
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

    # EBS 扩充字段
    shipment_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ship_to_location_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    receiving_routing_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    qty_rcv_tolerance: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    quantity_received: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    quantity_accepted: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    quantity_rejected: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    quantity_billed: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    quantity_cancelled: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    inspection_required_flag: Mapped[Optional[str]] = mapped_column(
        String(1), nullable=True
    )
    receipt_required_flag: Mapped[Optional[str]] = mapped_column(
        String(1), nullable=True
    )
    closed_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # 关系
    line: Mapped[PoLine] = relationship(back_populates="locations")
    distributions: Mapped[list[PoDistribution]] = relationship(back_populates="location")


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
    transaction_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    vendor_id: Mapped[str] = mapped_column(String(20), nullable=False)

    # EBS 扩充字段
    po_header_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    po_line_location_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shipment_line_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("rcv_shipment_lines.shipment_line_id"), nullable=True
    )
    source_document_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    destination_type_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    unit_of_measure: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    primary_quantity: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    inspection_status_code: Mapped[Optional[str]] = mapped_column(
        String(25), nullable=True
    )
    parent_transaction_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # 关系
    po_line: Mapped[PoLine] = relationship(back_populates="rcv_transactions")
    shipment_line: Mapped[Optional[RcvShipmentLine]] = relationship(
        back_populates="transactions",
        foreign_keys=[shipment_line_id],
        primaryjoin="RcvTransaction.shipment_line_id == RcvShipmentLine.shipment_line_id",
    )


class ApInvoice(Base):
    """发票（AP_INVOICES_ALL）。"""

    __tablename__ = "ap_invoices"

    invoice_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_num: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    po_number: Mapped[str] = mapped_column(String(30), nullable=False)
    vendor_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("ap_suppliers.vendor_id"), nullable=False
    )
    vendor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    invoice_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    invoice_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    discount_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    approval_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="VALIDATED"
    )
    terms_id: Mapped[str] = mapped_column(String(20), nullable=False, default="NET30")

    # EBS 扩充字段
    invoice_type_lookup_code: Mapped[Optional[str]] = mapped_column(
        String(25), nullable=True
    )
    invoice_currency_code: Mapped[Optional[str]] = mapped_column(
        String(15), nullable=True
    )
    exchange_rate: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 5), nullable=True
    )
    amount_paid: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    discount_amount_taken: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    base_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    pay_group_lookup_code: Mapped[Optional[str]] = mapped_column(
        String(25), nullable=True
    )
    source: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    org_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cancelled_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    cancelled_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    gl_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    goods_received_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    invoice_received_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # 关系
    supplier: Mapped[ApSupplier] = relationship(back_populates="invoices")
    invoice_lines: Mapped[list[ApInvoiceLine]] = relationship(back_populates="invoice")
    invoice_distributions: Mapped[list[ApInvoiceDistribution]] = relationship(
        back_populates="invoice"
    )
    invoice_payments: Mapped[list[ApInvoicePayment]] = relationship(
        back_populates="invoice"
    )
    payment_schedules: Mapped[list[ApPaymentSchedule]] = relationship(
        back_populates="invoice"
    )


class ApPayment(Base):
    """付款（AP_CHECKS_ALL）。"""

    __tablename__ = "ap_checks"

    check_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    check_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    invoice_num: Mapped[str] = mapped_column(String(30), nullable=False)
    vendor_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("ap_suppliers.vendor_id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    check_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payment_method_code: Mapped[str] = mapped_column(String(20), nullable=False, default="BANK_TRANSFER")

    # EBS 扩充字段
    bank_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    currency_code: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    exchange_rate: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 5), nullable=True
    )
    status_lookup_code: Mapped[Optional[str]] = mapped_column(
        String(25), nullable=True
    )
    cleared_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    cleared_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    void_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    org_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # 关系
    supplier: Mapped[ApSupplier] = relationship(back_populates="payments")
    invoice_payments: Mapped[list[ApInvoicePayment]] = relationship(
        back_populates="check"
    )


# ---------------------------------------------------------------------------
# 采购域 — 新增表
# ---------------------------------------------------------------------------


class PoDistribution(Base):
    """采购订单分配（PO_DISTRIBUTIONS_ALL）。"""

    __tablename__ = "po_distributions"

    po_distribution_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_header_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("po_headers.po_header_id"), nullable=False
    )
    po_line_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("po_lines.po_line_id"), nullable=False
    )
    line_location_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("po_line_locations.line_location_id"), nullable=False
    )
    set_of_books_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    code_combination_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    quantity_ordered: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    quantity_delivered: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    quantity_billed: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    quantity_cancelled: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    amount_billed: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    gl_encumbered_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    budget_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    destination_type_code: Mapped[Optional[str]] = mapped_column(
        String(25), nullable=True
    )
    org_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    header: Mapped[PoHeader] = relationship(back_populates="distributions")
    line: Mapped[PoLine] = relationship(back_populates="distributions")
    location: Mapped[PoLineLocation] = relationship(back_populates="distributions")


# ---------------------------------------------------------------------------
# 收货域 — 新增表
# ---------------------------------------------------------------------------


class RcvShipmentHeader(Base):
    """收货单头（RCV_SHIPMENT_HEADERS）。"""

    __tablename__ = "rcv_shipment_headers"

    shipment_header_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_num: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    vendor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    vendor_site_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ship_to_org_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shipped_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    expected_receipt_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    receipt_source_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    shipment_num: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    waybill_airbill_num: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    freight_carrier_code: Mapped[Optional[str]] = mapped_column(
        String(25), nullable=True
    )
    packing_slip: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    comments: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    lines: Mapped[list[RcvShipmentLine]] = relationship(back_populates="header")


class RcvShipmentLine(Base):
    """收货单行（RCV_SHIPMENT_LINES）。"""

    __tablename__ = "rcv_shipment_lines"

    shipment_line_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shipment_header_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("rcv_shipment_headers.shipment_header_id"),
        nullable=False,
    )
    line_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    po_header_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    po_line_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    po_line_location_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    item_description: Mapped[Optional[str]] = mapped_column(String(240), nullable=True)
    quantity_shipped: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    quantity_received: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    unit_of_measure: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    category_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    vendor_item_num: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    header: Mapped[RcvShipmentHeader] = relationship(back_populates="lines")
    transactions: Mapped[list[RcvTransaction]] = relationship(
        back_populates="shipment_line",
        foreign_keys="[RcvTransaction.shipment_line_id]",
    )


# ---------------------------------------------------------------------------
# 应付域 — 新增表
# ---------------------------------------------------------------------------


class ApInvoiceLine(Base):
    """发票行（AP_INVOICE_LINES_ALL）。"""

    __tablename__ = "ap_invoice_lines"

    invoice_line_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ap_invoices.invoice_id"), nullable=False
    )
    line_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    line_type_lookup_code: Mapped[Optional[str]] = mapped_column(
        String(25), nullable=True
    )
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    quantity_invoiced: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    unit_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    po_header_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    po_line_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    po_line_location_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    po_distribution_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    inventory_item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    item_description: Mapped[Optional[str]] = mapped_column(String(240), nullable=True)
    accounting_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discarded_flag: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    cancelled_flag: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    invoice: Mapped[ApInvoice] = relationship(back_populates="invoice_lines")


class ApInvoiceDistribution(Base):
    """发票分配（AP_INVOICE_DISTRIBUTIONS_ALL）。"""

    __tablename__ = "ap_invoice_distributions"

    invoice_distribution_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ap_invoices.invoice_id"), nullable=False
    )
    invoice_line_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    distribution_line_number: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    line_type_lookup_code: Mapped[Optional[str]] = mapped_column(
        String(25), nullable=True
    )
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    base_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    quantity_invoiced: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    po_distribution_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    set_of_books_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    accounting_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    dist_code_combination_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    match_status_flag: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    posted_flag: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    invoice: Mapped[ApInvoice] = relationship(back_populates="invoice_distributions")


class ApInvoicePayment(Base):
    """发票付款关联（AP_INVOICE_PAYMENTS_ALL）。"""

    __tablename__ = "ap_invoice_payments"

    invoice_payment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ap_invoices.invoice_id"), nullable=False
    )
    check_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ap_checks.check_id"), nullable=False
    )
    payment_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    discount_taken: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    discount_lost: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    invoice_base_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    payment_base_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    accounting_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    reversal_flag: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    reversal_inv_pmt_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    invoice: Mapped[ApInvoice] = relationship(back_populates="invoice_payments")
    check: Mapped[ApPayment] = relationship(back_populates="invoice_payments")


class ApPaymentSchedule(Base):
    """付款计划（AP_PAYMENT_SCHEDULES_ALL）。"""

    __tablename__ = "ap_payment_schedules"

    payment_schedule_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ap_invoices.invoice_id"), nullable=False
    )
    payment_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    discount_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    gross_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    amount_remaining: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    discount_amount_available: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    payment_priority: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hold_flag: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    payment_status_flag: Mapped[Optional[str]] = mapped_column(
        String(1), nullable=True
    )
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    invoice: Mapped[ApInvoice] = relationship(back_populates="payment_schedules")


# ---------------------------------------------------------------------------
# 主数据域 — 新增表
# ---------------------------------------------------------------------------


class ApSupplierSite(Base):
    """供应商地点（AP_SUPPLIER_SITES_ALL）。"""

    __tablename__ = "ap_supplier_sites"

    vendor_site_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("ap_suppliers.vendor_id"), nullable=False
    )
    vendor_site_code: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    address_line1: Mapped[Optional[str]] = mapped_column(String(240), nullable=True)
    address_line2: Mapped[Optional[str]] = mapped_column(String(240), nullable=True)
    address_line3: Mapped[Optional[str]] = mapped_column(String(240), nullable=True)
    address_line4: Mapped[Optional[str]] = mapped_column(String(240), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    zip: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    fax: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    email_address: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    terms_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pay_group_lookup_code: Mapped[Optional[str]] = mapped_column(
        String(25), nullable=True
    )
    payment_method_lookup_code: Mapped[Optional[str]] = mapped_column(
        String(25), nullable=True
    )
    org_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    purchasing_site_flag: Mapped[Optional[str]] = mapped_column(
        String(1), nullable=True
    )
    pay_site_flag: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    inactive_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    supplier: Mapped[ApSupplier] = relationship(back_populates="sites")


class MtlSystemItem(Base):
    """物料主数据（MTL_SYSTEM_ITEMS_B）。"""

    __tablename__ = "mtl_system_items"

    inventory_item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    segment1: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(240), nullable=True)
    primary_uom_code: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    item_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    buyer_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    list_price_per_unit: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    purchasing_item_flag: Mapped[Optional[str]] = mapped_column(
        String(1), nullable=True
    )
    purchasing_enabled_flag: Mapped[Optional[str]] = mapped_column(
        String(1), nullable=True
    )
    inventory_item_status_code: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True
    )
    item_catalog_group_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )


# ---------------------------------------------------------------------------
# 寻源/合同域 — 新增表
# ---------------------------------------------------------------------------


class PonAuctionHeader(Base):
    """寻源事件头（PON_AUCTION_HEADERS_ALL）。"""

    __tablename__ = "pon_auction_headers"

    auction_header_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_number: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    auction_title: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    auction_type: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    auction_status: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    open_bidding_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    close_bidding_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    outcome: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    contract_type: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    org_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    bids: Mapped[list[PonBidHeader]] = relationship(back_populates="auction")


class PonBidHeader(Base):
    """投标头（PON_BID_HEADERS）。"""

    __tablename__ = "pon_bid_headers"

    bid_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    auction_header_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("pon_auction_headers.auction_header_id"),
        nullable=False,
    )
    bid_status: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    vendor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    vendor_site_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bid_total: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    bid_currency_code: Mapped[Optional[str]] = mapped_column(
        String(15), nullable=True
    )
    publish_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    award_status: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    award_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    auction: Mapped[PonAuctionHeader] = relationship(back_populates="bids")


class OkcKHeader(Base):
    """合同头（OKC_K_HEADERS_B）。"""

    __tablename__ = "okc_k_headers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_number: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    contract_number_modifier: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True
    )
    sts_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    estimated_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    currency_code: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    authoring_org_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    buy_or_sell: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    scs_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    short_description: Mapped[Optional[str]] = mapped_column(
        String(600), nullable=True
    )
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    lines: Mapped[list[OkcKLine]] = relationship(back_populates="header")


class OkcKLine(Base):
    """合同行（OKC_K_LINES_B）。"""

    __tablename__ = "okc_k_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chr_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("okc_k_headers.id"), nullable=False
    )
    line_number: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    sts_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    item_description: Mapped[Optional[str]] = mapped_column(String(240), nullable=True)
    price_unit: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    price_negotiated: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    uom_code: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    last_update_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # 关系
    header: Mapped[OkcKHeader] = relationship(back_populates="lines")
