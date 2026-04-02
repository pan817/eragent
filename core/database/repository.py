"""
P2P 数据访问层（Repository）。

提供 SQL 查询方法，替代原有的内存 mock 数据过滤。
查询结果格式与原 tools.py 中 _get_mock_data() 输出完全一致，
确保规则引擎无需任何改动。
"""

from __future__ import annotations

from typing import Any

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


class P2PRepository:
    """P2P 业务数据查询仓库，所有方法直接执行 SQL 查询。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ================================================================
    # 查询工具用：直接返回业务对象列表
    # ================================================================

    def query_purchase_orders(
        self,
        supplier_id: str = "",
        status: str = "",
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """查询采购订单（扁平化格式，供查询工具使用）。"""
        with self._session_factory() as session:
            stmt = (
                select(
                    PoHeader.po_number,
                    PoHeader.supplier_id,
                    PoHeader.supplier_name,
                    PoLine.category.label("material_category"),
                    PoHeader.total_amount.label("po_amount"),
                    PoLine.quantity.label("po_quantity"),
                    PoLine.unit_price,
                    PoLine.standard_price.label("contract_price"),
                    PoHeader.status,
                    PoHeader.creation_date,
                    PoLineLocation.promised_date.label("required_date"),
                    PoLine.item_code.label("material_code"),
                    PoLine.item_description.label("material_name"),
                    PoLine.line_num.label("line_number"),
                )
                .join(PoLine, PoHeader.po_header_id == PoLine.po_header_id)
                .join(PoLineLocation, PoLine.po_line_id == PoLineLocation.po_line_id)
            )

            if supplier_id:
                stmt = stmt.where(PoHeader.supplier_id == supplier_id)
            if status:
                stmt = stmt.where(PoHeader.status == status.upper())

            rows = session.execute(stmt).all()
            return [
                {
                    "po_number": r.po_number,
                    "supplier_id": r.supplier_id,
                    "supplier_name": r.supplier_name,
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
        supplier_id: str = "",
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """查询收货记录。"""
        with self._session_factory() as session:
            stmt = select(RcvTransaction)

            if po_number:
                stmt = stmt.where(RcvTransaction.po_number == po_number)
            if supplier_id:
                stmt = stmt.where(RcvTransaction.supplier_id == supplier_id)

            rows = session.scalars(stmt).all()
            return [
                {
                    "receipt_id": f"GR-{r.transaction_id:04d}",
                    "gr_number": f"GR-{r.transaction_id:04d}",
                    "po_number": r.po_number,
                    "supplier_id": r.supplier_id,
                    "gr_quantity": float(r.quantity),
                    "receipt_date": r.transaction_date.isoformat(),
                    "quality_passed": r.rejected_quantity == 0,
                }
                for r in rows
            ]

    def query_invoices(
        self,
        po_number: str = "",
        supplier_id: str = "",
        status: str = "",
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """查询发票数据。"""
        with self._session_factory() as session:
            stmt = select(ApInvoice)

            if po_number:
                stmt = stmt.where(ApInvoice.po_number == po_number)
            if supplier_id:
                stmt = stmt.where(ApInvoice.supplier_id == supplier_id)
            if status:
                stmt = stmt.where(ApInvoice.status == status.upper())

            rows = session.scalars(stmt).all()
            return [
                {
                    "invoice_number": r.invoice_number,
                    "po_number": r.po_number,
                    "supplier_id": r.supplier_id,
                    "supplier_name": r.supplier_name,
                    "invoice_amount": float(r.invoice_amount),
                    "due_date": r.due_date.isoformat(),
                    "discount_due_date": r.discount_due_date.isoformat() if r.discount_due_date else "",
                    "discount_amount": float(r.invoice_amount) * 0.98,
                    "status": r.status.lower(),
                    "creation_date": r.invoice_date.isoformat(),
                }
                for r in rows
            ]

    def query_payments(
        self,
        invoice_number: str = "",
        supplier_id: str = "",
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """查询付款记录。"""
        with self._session_factory() as session:
            stmt = select(ApPayment)

            if invoice_number:
                stmt = stmt.where(ApPayment.invoice_number == invoice_number)
            if supplier_id:
                stmt = stmt.where(ApPayment.supplier_id == supplier_id)

            rows = session.scalars(stmt).all()
            return [
                {
                    "payment_id": r.payment_number,
                    "payment_number": r.payment_number,
                    "invoice_number": r.invoice_number,
                    "supplier_id": r.supplier_id,
                    "payment_amount": float(r.payment_amount),
                    "payment_date": r.payment_date.isoformat(),
                    "payment_method": r.payment_method.lower(),
                }
                for r in rows
            ]

    # ================================================================
    # 分析工具用：SQL JOIN 输出规则引擎期望的扁平化格式
    # ================================================================

    def get_flattened_purchase_orders(
        self,
        supplier_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
        """获取扁平化的采购订单数据（供规则引擎使用）。

        等同于原 _get_mock_data()["purchase_orders"] 的格式。
        """
        result = self.query_purchase_orders(supplier_id=supplier_id)
        if po_number:
            result = [po for po in result if po["po_number"] == po_number]
        return result

    def get_flattened_receipts(
        self,
        supplier_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
        """获取扁平化的收货数据（供规则引擎使用）。"""
        return self.query_receipts(po_number=po_number, supplier_id=supplier_id)

    def get_flattened_invoices(
        self,
        supplier_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
        """获取扁平化的发票数据（供规则引擎使用）。"""
        return self.query_invoices(po_number=po_number, supplier_id=supplier_id)

    def get_flattened_payments(
        self,
        supplier_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
        """获取扁平化的付款数据（供规则引擎使用）。"""
        result = self.query_payments(supplier_id=supplier_id)
        if po_number:
            # 需要通过发票关联 PO
            with self._session_factory() as session:
                inv_nums = session.scalars(
                    select(ApInvoice.invoice_number).where(
                        ApInvoice.po_number == po_number
                    )
                ).all()
            result = [p for p in result if p["invoice_number"] in set(inv_nums)]
        return result

    def get_contract_prices(self) -> dict[str, float]:
        """获取物料合同价格映射（item_code -> standard_price）。"""
        with self._session_factory() as session:
            rows = session.execute(
                select(PoLine.item_code, PoLine.standard_price).distinct()
            ).all()
            return {r.item_code: float(r.standard_price) for r in rows}
