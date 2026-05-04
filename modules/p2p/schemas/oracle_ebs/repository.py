"""Oracle EBS P2P Repository — implements P2PRepositoryProtocol for Oracle EBS schema."""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from core.logging_utils import get_logger
from modules.p2p.schemas.oracle_ebs.models import (
    ApInvoice,
    ApPayment,
    ApSupplier,
    PoHeader,
    PoLine,
    PoLineLocation,
    RcvTransaction,
)

_logger = get_logger(__name__)
_CONTRACT_PRICE_TTL_SECONDS = 3600.0


def _apply_order_and_limit(
    stmt: Any,
    order_by: str,
    limit: int,
    *,
    date_col: Any,
    amount_col: Any | None,
    max_limit: int = 0,
) -> Any:
    if order_by == "date_desc":
        stmt = stmt.order_by(date_col.desc())
    elif order_by == "date_asc":
        stmt = stmt.order_by(date_col.asc())
    elif order_by == "amount_desc" and amount_col is not None:
        stmt = stmt.order_by(amount_col.desc())
    elif order_by == "amount_asc" and amount_col is not None:
        stmt = stmt.order_by(amount_col.asc())

    if max_limit > 0:
        effective = min(limit, max_limit) if limit > 0 else max_limit
    else:
        effective = limit
    if effective > 0:
        stmt = stmt.limit(effective)

    return stmt


class OracleEBSRepository:
    """Oracle EBS P2P data access repository."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._contract_price_cache: dict[str, float] | None = None
        self._contract_price_cache_at: float = 0.0
        self._contract_price_lock = threading.Lock()

    # ================================================================
    # Basic queries
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
    # Flattened queries (rule engine)
    # ================================================================

    def get_flattened_purchase_orders(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
        return self.query_purchase_orders(
            vendor_id=vendor_id, po_number=po_number, days=0
        )

    def get_flattened_receipts(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
        return self.query_receipts(po_number=po_number, vendor_id=vendor_id, days=0)

    def get_flattened_invoices(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
        return self.query_invoices(po_number=po_number, vendor_id=vendor_id, days=0)

    def get_flattened_payments(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]:
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

    def query_suppliers(
        self,
        vendor_id: str = "",
    ) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(ApSupplier)
            if vendor_id:
                stmt = stmt.where(ApSupplier.vendor_id == vendor_id)
            rows = session.scalars(stmt).all()
            return [
                {
                    "vendor_id": r.vendor_id,
                    "vendor_name": r.vendor_name,
                    "segment1": r.segment1,
                    "vendor_type": getattr(r, "vendor_type_lookup_code", ""),
                    "terms_id": str(getattr(r, "terms_id", "")),
                    "enabled_flag": getattr(r, "enabled_flag", "Y"),
                }
                for r in rows
            ]

    def get_contract_prices(self) -> dict[str, float]:
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
        with self._contract_price_lock:
            self._contract_price_cache = None
            self._contract_price_cache_at = 0.0

    # ================================================================
    # Aggregate analysis (sunk from advanced.py)
    # ================================================================

    def analyze_receipt_anomalies(
        self,
        vendor_id: str = "",
        po_number: str = "",
        days: int = 30,
    ) -> list[dict[str, Any]]:
        cutoff = date.today() - timedelta(days=days)
        anomalies: list[dict[str, Any]] = []

        with self._session_factory() as session:
            stmt = select(RcvTransaction)
            if vendor_id:
                stmt = stmt.where(RcvTransaction.vendor_id == vendor_id)
            if po_number:
                stmt = stmt.where(RcvTransaction.po_number == po_number)
            stmt = stmt.where(RcvTransaction.transaction_date >= cutoff)
            receipts = session.scalars(stmt).all()

            for rcv in receipts:
                po_line = session.get(PoLine, rcv.po_line_id)
                if po_line is None:
                    continue

                issues: list[str] = []

                if rcv.quantity > po_line.quantity:
                    over_pct = ((rcv.quantity - po_line.quantity) / po_line.quantity) * 100
                    issues.append(f"超量收货 {over_pct:.1f}%")

                if rcv.rejected_quantity > 0:
                    reject_pct = (rcv.rejected_quantity / rcv.quantity) * 100
                    issues.append(f"拒收 {rcv.rejected_quantity} 件 ({reject_pct:.1f}%)")

                loc = session.execute(
                    select(PoLineLocation)
                    .where(PoLineLocation.po_line_id == rcv.po_line_id)
                    .limit(1)
                ).scalar()
                if loc and rcv.transaction_date.date() > loc.promised_date:
                    delay = (rcv.transaction_date.date() - loc.promised_date).days
                    issues.append(f"延迟 {delay} 天")

                if issues:
                    anomalies.append({
                        "receipt_id": f"GR-{rcv.transaction_id:04d}",
                        "po_number": rcv.po_number,
                        "vendor_id": rcv.vendor_id,
                        "po_quantity": float(po_line.quantity),
                        "received_quantity": float(rcv.quantity),
                        "rejected_quantity": float(rcv.rejected_quantity),
                        "receipt_date": rcv.transaction_date.isoformat(),
                        "issues": issues,
                    })

        return anomalies

    def detect_duplicate_invoices(
        self,
        vendor_id: str = "",
        days: int = 30,
    ) -> list[dict[str, Any]]:
        cutoff = date.today() - timedelta(days=days)
        duplicates: list[dict[str, Any]] = []

        with self._session_factory() as session:
            stmt = select(ApInvoice).where(ApInvoice.invoice_date >= cutoff)
            if vendor_id:
                stmt = stmt.where(ApInvoice.vendor_id == vendor_id)
            invoices = session.scalars(stmt).all()

            groups: dict[tuple[str, float], list[Any]] = {}
            for inv in invoices:
                key = (inv.vendor_id, float(inv.invoice_amount))
                groups.setdefault(key, []).append(inv)

            for (sid, amount), group in groups.items():
                if len(group) < 2:
                    continue
                group.sort(key=lambda x: x.invoice_date)
                for i in range(len(group) - 1):
                    for j in range(i + 1, len(group)):
                        gap = abs((group[j].invoice_date - group[i].invoice_date).days)
                        if gap <= 3:
                            duplicates.append({
                                "vendor_id": sid,
                                "vendor_name": group[i].vendor_name,
                                "amount": amount,
                                "invoice_a": group[i].invoice_num,
                                "invoice_b": group[j].invoice_num,
                                "date_a": group[i].invoice_date.isoformat(),
                                "date_b": group[j].invoice_date.isoformat(),
                                "date_gap_days": gap,
                            })

        return duplicates

    def analyze_discount_utilization(
        self,
        vendor_id: str = "",
        days: int = 30,
    ) -> dict[str, Any]:
        cutoff = date.today() - timedelta(days=days)
        discount_rate = 0.02

        with self._session_factory() as session:
            stmt = (
                select(ApInvoice)
                .where(
                    ApInvoice.invoice_date >= cutoff,
                    ApInvoice.discount_due_date.isnot(None),
                )
            )
            if vendor_id:
                stmt = stmt.where(ApInvoice.vendor_id == vendor_id)
            invoices = session.scalars(stmt).all()

            total_eligible = len(invoices)
            utilized = 0
            missed = 0
            missed_amount = 0.0
            details: list[dict[str, Any]] = []

            for inv in invoices:
                payment = session.execute(
                    select(ApPayment)
                    .where(ApPayment.invoice_num == inv.invoice_num)
                    .limit(1)
                ).scalar()

                if payment is None:
                    continue

                potential_saving = float(inv.invoice_amount) * discount_rate
                if payment.check_date.date() <= inv.discount_due_date:
                    utilized += 1
                else:
                    missed += 1
                    missed_amount += potential_saving
                    details.append({
                        "invoice_num": inv.invoice_num,
                        "vendor_name": inv.vendor_name,
                        "invoice_amount": float(inv.invoice_amount),
                        "discount_due_date": inv.discount_due_date.isoformat(),
                        "check_date": payment.check_date.isoformat(),
                        "days_late": (payment.check_date.date() - inv.discount_due_date).days,
                        "missed_saving": round(potential_saving, 2),
                    })

            utilization_rate = (utilized / total_eligible * 100) if total_eligible > 0 else 0

        return {
            "total_eligible": total_eligible,
            "utilized": utilized,
            "missed": missed,
            "utilization_rate": round(utilization_rate, 1),
            "total_missed_saving": round(missed_amount, 2),
            "missed_details": details[:20],
        }

    def analyze_vendor_concentration(
        self,
        days: int = 30,
        top_n: int = 10,
    ) -> dict[str, Any]:
        cutoff = date.today() - timedelta(days=days)

        with self._session_factory() as session:
            stmt = (
                select(
                    PoHeader.vendor_id,
                    PoHeader.vendor_name,
                    func.sum(PoHeader.total_amount).label("total_spend"),
                    func.count(func.distinct(PoHeader.po_number)).label("order_count"),
                )
                .where(PoHeader.creation_date >= cutoff)
                .group_by(PoHeader.vendor_id, PoHeader.vendor_name)
                .order_by(func.sum(PoHeader.total_amount).desc())
            )
            vendor_rows = session.execute(stmt).all()

            grand_total = sum(float(r.total_spend or 0) for r in vendor_rows)
            vendors = []
            high_dependency: list[dict[str, Any]] = []
            for r in vendor_rows[:top_n]:
                spend = float(r.total_spend or 0)
                pct = (spend / grand_total * 100) if grand_total > 0 else 0
                entry = {
                    "vendor_id": r.vendor_id,
                    "vendor_name": r.vendor_name,
                    "total_spend": round(spend, 2),
                    "order_count": r.order_count,
                    "spend_pct": round(pct, 1),
                }
                vendors.append(entry)
                if pct > 20:
                    high_dependency.append(entry)

            cat_stmt = (
                select(
                    PoLine.category_id,
                    func.count(func.distinct(PoHeader.vendor_id)).label("vendor_count"),
                )
                .join(PoHeader, PoHeader.po_header_id == PoLine.po_header_id)
                .where(PoHeader.creation_date >= cutoff)
                .group_by(PoLine.category_id)
                .having(func.count(func.distinct(PoHeader.vendor_id)) == 1)
            )
            single_source = session.execute(cat_stmt).all()
            single_source_categories = [r.category_id for r in single_source]

        return {
            "grand_total_spend": round(grand_total, 2),
            "top_vendors": vendors,
            "high_dependency_vendors": high_dependency,
            "single_source_categories": single_source_categories,
        }

    def calculate_po_cycle_time(
        self,
        days: int = 30,
        vendor_id: str = "",
    ) -> dict[str, Any]:
        cutoff = date.today() - timedelta(days=days)

        with self._session_factory() as session:
            stmt = select(PoHeader).where(PoHeader.creation_date >= cutoff)
            if vendor_id:
                stmt = stmt.where(PoHeader.vendor_id == vendor_id)
            orders = session.scalars(stmt).all()

            details: list[dict[str, Any]] = []
            total_c2r = 0
            total_r2p = 0
            total_e2e = 0
            n_rcv = 0
            n_pmt = 0

            for po in orders:
                entry: dict[str, Any] = {
                    "po_number": po.po_number,
                    "vendor_name": po.vendor_name,
                    "created_date": po.creation_date.isoformat(),
                }

                first_receipt = session.execute(
                    select(func.min(RcvTransaction.transaction_date))
                    .where(RcvTransaction.po_number == po.po_number)
                ).scalar()

                if first_receipt:
                    c2r = (first_receipt - po.creation_date).days
                    entry["first_receipt_date"] = first_receipt.isoformat()
                    entry["create_to_receipt_days"] = c2r
                    total_c2r += c2r
                    n_rcv += 1

                    first_payment_date = session.execute(
                        select(func.min(ApPayment.check_date))
                        .join(ApInvoice, ApPayment.invoice_num == ApInvoice.invoice_num)
                        .where(ApInvoice.po_number == po.po_number)
                    ).scalar()

                    if first_payment_date:
                        r2p = (first_payment_date - first_receipt).days
                        e2e = (first_payment_date - po.creation_date).days
                        entry["first_payment_date"] = first_payment_date.isoformat()
                        entry["receipt_to_payment_days"] = r2p
                        entry["end_to_end_days"] = e2e
                        total_r2p += r2p
                        total_e2e += e2e
                        n_pmt += 1

                details.append(entry)

        return {
            "total_orders": len(details),
            "avg_create_to_receipt_days": round(total_c2r / n_rcv, 1) if n_rcv else None,
            "avg_receipt_to_payment_days": round(total_r2p / n_pmt, 1) if n_pmt else None,
            "avg_end_to_end_days": round(total_e2e / n_pmt, 1) if n_pmt else None,
            "orders_with_receipt": n_rcv,
            "orders_with_payment": n_pmt,
            "details": details[:30],
        }
