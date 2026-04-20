"""
P2P 数据访问层（Repository）。

提供 SQL 查询方法，替代原有的内存 mock 数据过滤。
查询结果格式与原 tools.py 中 _get_mock_data() 输出完全一致，
确保规则引擎无需任何改动。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import threading
import time

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from core.database.models import (
    ApInvoice,
    ApPayment,
    ApSupplier,
    PoHeader,
    PoLine,
    PoLineLocation,
    RcvTransaction,
)
from core.logging_utils import get_logger

_logger = get_logger(__name__)
_CONTRACT_PRICE_TTL_SECONDS = 3600.0


def _apply_order_and_limit(
    stmt: Any,
    order_by: str,
    limit: int,
    *,
    date_col: Any,
    amount_col: Any | None,
) -> Any:
    """为 SELECT 语句追加 ORDER BY 和 LIMIT 子句。

    Args:
        stmt: SQLAlchemy Select 语句。
        order_by: 排序方式枚举值（date_desc/date_asc/amount_desc/amount_asc）。
        limit: 结果数量上限，0 表示不限制。
        date_col: 日期排序列（必须提供）。
        amount_col: 金额排序列（可选，为 None 时 amount 排序静默跳过）。
    """
    if order_by == "date_desc":
        stmt = stmt.order_by(date_col.desc())
    elif order_by == "date_asc":
        stmt = stmt.order_by(date_col.asc())
    elif order_by == "amount_desc" and amount_col is not None:
        stmt = stmt.order_by(amount_col.desc())
    elif order_by == "amount_asc" and amount_col is not None:
        stmt = stmt.order_by(amount_col.asc())

    if limit > 0:
        stmt = stmt.limit(limit)

    return stmt


class P2PRepository:
    """P2P 业务数据查询仓库，所有方法直接执行 SQL 查询。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._contract_price_cache: dict[str, float] | None = None
        self._contract_price_cache_at: float = 0.0
        self._contract_price_lock = threading.Lock()

    # ================================================================
    # 查询工具用：直接返回业务对象列表
    # ================================================================

    def query_purchase_orders(
        self,
        vendor_id: str = "",
        status: str = "",
        days: int = 30,
        po_number: str = "",
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]:
        """查询采购订单（扁平化格式，供查询工具使用）。"""
        with self._session_factory() as session:
            stmt = (
                select(
                    PoHeader.po_number,
                    PoHeader.vendor_id,
                    PoHeader.vendor_name,
                    PoLine.category_id.label("material_category"),
                    PoHeader.total_amount.label("po_amount"),
                    PoLine.quantity.label("po_quantity"),
                    PoLine.unit_price,
                    PoLine.standard_price.label("contract_price"),
                    PoHeader.status,
                    PoHeader.creation_date,
                    PoLineLocation.promised_date.label("required_date"),
                    PoLine.item_id.label("material_code"),
                    PoLine.item_description.label("material_name"),
                    PoLine.line_num.label("line_number"),
                )
                .join(PoLine, PoHeader.po_header_id == PoLine.po_header_id)
                .join(PoLineLocation, PoLine.po_line_id == PoLineLocation.po_line_id)
            )

            if vendor_id:
                stmt = stmt.where(PoHeader.vendor_id == vendor_id)
            if status:
                stmt = stmt.where(PoHeader.status == status.upper())
            if po_number:
                stmt = stmt.where(PoHeader.po_number == po_number)
            if days > 0:
                stmt = stmt.where(
                    PoHeader.creation_date >= date.today() - timedelta(days=days)
                )

            stmt = _apply_order_and_limit(
                stmt, order_by, limit,
                date_col=PoHeader.creation_date,
                amount_col=PoHeader.total_amount,
            )

            rows = session.execute(stmt).all()
            return [
                {
                    "po_number": r.po_number,
                    "vendor_id": r.vendor_id,
                    "vendor_name": r.vendor_name,
                    "material_category": r.material_category,
                    "po_amount": float(r.po_amount),
                    "po_quantity": float(r.po_quantity),
                    "unit_price": float(r.unit_price),
                    "contract_price": float(r.contract_price),
                    "status": r.status.lower(),
                    "creation_date": r.creation_date.isoformat(),
                    "required_date": r.required_date.isoformat(),
                    "material_code": r.material_code,
                    "material_name": r.material_name,
                    "line_number": str(r.line_number),
                }
                for r in rows
            ]

    def query_receipts(
        self,
        po_number: str = "",
        vendor_id: str = "",
        days: int = 30,
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]:
        """查询收货记录。"""
        with self._session_factory() as session:
            stmt = select(RcvTransaction)

            if po_number:
                stmt = stmt.where(RcvTransaction.po_number == po_number)
            if vendor_id:
                stmt = stmt.where(RcvTransaction.vendor_id == vendor_id)
            if days > 0:
                stmt = stmt.where(
                    RcvTransaction.transaction_date >= date.today() - timedelta(days=days)
                )

            stmt = _apply_order_and_limit(
                stmt, order_by, limit,
                date_col=RcvTransaction.transaction_date,
                amount_col=None,
            )

            rows = session.scalars(stmt).all()
            return [
                {
                    "receipt_id": f"GR-{r.transaction_id:04d}",
                    "gr_number": f"GR-{r.transaction_id:04d}",
                    "po_number": r.po_number,
                    "vendor_id": r.vendor_id,
                    "gr_quantity": float(r.quantity),
                    "receipt_date": r.transaction_date.isoformat(),
                    "quality_passed": r.rejected_quantity == 0,
                }
                for r in rows
            ]

    def query_invoices(
        self,
        po_number: str = "",
        vendor_id: str = "",
        status: str = "",
        invoice_num: str = "",
        days: int = 30,
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]:
        """查询发票数据。"""
        with self._session_factory() as session:
            stmt = select(ApInvoice)

            if invoice_num:
                stmt = stmt.where(ApInvoice.invoice_num == invoice_num)
            if po_number:
                stmt = stmt.where(ApInvoice.po_number == po_number)
            if vendor_id:
                stmt = stmt.where(ApInvoice.vendor_id == vendor_id)
            if status:
                stmt = stmt.where(ApInvoice.approval_status == status.upper())
            if days > 0:
                stmt = stmt.where(
                    ApInvoice.invoice_date >= date.today() - timedelta(days=days)
                )

            stmt = _apply_order_and_limit(
                stmt, order_by, limit,
                date_col=ApInvoice.invoice_date,
                amount_col=ApInvoice.invoice_amount,
            )

            rows = session.scalars(stmt).all()
            return [
                {
                    "invoice_num": r.invoice_num,
                    "po_number": r.po_number,
                    "vendor_id": r.vendor_id,
                    "vendor_name": r.vendor_name,
                    "invoice_amount": float(r.invoice_amount),
                    "due_date": r.due_date.isoformat(),
                    "discount_due_date": r.discount_due_date.isoformat() if r.discount_due_date else "",
                    "discount_amount": float(r.invoice_amount) * 0.98,
                    "approval_status": r.approval_status.lower(),
                    "creation_date": r.invoice_date.isoformat(),
                }
                for r in rows
            ]

    def query_payments(
        self,
        invoice_num: str = "",
        vendor_id: str = "",
        check_number: str = "",
        days: int = 30,
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]:
        """查询付款记录。"""
        with self._session_factory() as session:
            stmt = select(ApPayment)

            if check_number:
                stmt = stmt.where(ApPayment.check_number == check_number)
            if invoice_num:
                stmt = stmt.where(ApPayment.invoice_num == invoice_num)
            if vendor_id:
                stmt = stmt.where(ApPayment.vendor_id == vendor_id)
            if days > 0:
                stmt = stmt.where(
                    ApPayment.check_date >= date.today() - timedelta(days=days)
                )

            stmt = _apply_order_and_limit(
                stmt, order_by, limit,
                date_col=ApPayment.check_date,
                amount_col=ApPayment.amount,
            )

            rows = session.scalars(stmt).all()
            return [
                {
                    "check_id": r.check_id,
                    "check_number": r.check_number,
                    "invoice_num": r.invoice_num,
                    "vendor_id": r.vendor_id,
                    "amount": float(r.amount),
                    "check_date": r.check_date.isoformat(),
                    "payment_method_code": r.payment_method_code.lower(),
                }
                for r in rows
            ]

    # ================================================================
    # 分析工具用：SQL JOIN 输出规则引擎期望的扁平化格式
    # ================================================================

    def get_flattened_purchase_orders(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
        """获取扁平化的采购订单数据（供规则引擎使用）。

        等同于原 _get_mock_data()["purchase_orders"] 的格式。
        ``po_number`` 已下推到 SQL WHERE，避免全表加载后内存过滤。
        """
        return self.query_purchase_orders(
            vendor_id=vendor_id, po_number=po_number, days=0
        )

    def get_flattened_receipts(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
        """获取扁平化的收货数据（供规则引擎使用）。"""
        return self.query_receipts(po_number=po_number, vendor_id=vendor_id, days=0)

    def get_flattened_invoices(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
        """获取扁平化的发票数据（供规则引擎使用）。"""
        return self.query_invoices(po_number=po_number, vendor_id=vendor_id, days=0)

    def get_flattened_payments(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
        """获取扁平化的付款数据（供规则引擎使用）。

        若提供 ``po_number``，使用单次 JOIN (Payment ⋈ Invoice) 拉取，
        避免「先查全部付款 + 再查发票号 + 内存过滤」的多轮 IO。
        """
        if not po_number:
            return self.query_payments(vendor_id=vendor_id, days=0)

        with self._session_factory() as session:
            stmt = (
                select(ApPayment)
                .join(ApInvoice, ApPayment.invoice_num == ApInvoice.invoice_num)
                .where(ApInvoice.po_number == po_number)
            )
            if vendor_id:
                stmt = stmt.where(ApPayment.vendor_id == vendor_id)
            rows = session.scalars(stmt).all()
            return [
                {
                    "check_id": r.check_id,
                    "check_number": r.check_number,
                    "invoice_num": r.invoice_num,
                    "vendor_id": r.vendor_id,
                    "amount": float(r.amount),
                    "check_date": r.check_date.isoformat(),
                    "payment_method_code": r.payment_method_code.lower(),
                }
                for r in rows
            ]

    def get_contract_prices(self) -> dict[str, float]:
        """获取物料合同价格映射（item_id -> standard_price）。

        进程内带 TTL 缓存（``_CONTRACT_PRICE_TTL_SECONDS``），
        避免每次规则调用都全表 DISTINCT。
        """
        now = time.monotonic()
        cache = self._contract_price_cache
        if (
            cache is not None
            and (now - self._contract_price_cache_at) < _CONTRACT_PRICE_TTL_SECONDS
        ):
            return cache
        with self._contract_price_lock:
            now = time.monotonic()
            if (
                self._contract_price_cache is not None
                and (now - self._contract_price_cache_at) < _CONTRACT_PRICE_TTL_SECONDS
            ):
                return self._contract_price_cache
            with self._session_factory() as session:
                rows = session.execute(
                    select(PoLine.item_id, PoLine.standard_price).distinct()
                ).all()
                prices = {r.item_id: float(r.standard_price) for r in rows}
            self._contract_price_cache = prices
            self._contract_price_cache_at = now
            return prices

    def invalidate_contract_price_cache(self) -> None:
        """显式失效合同价格缓存。"""
        with self._contract_price_lock:
            self._contract_price_cache = None
            self._contract_price_cache_at = 0.0
