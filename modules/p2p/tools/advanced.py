"""P2P 高级分析工具（5 个）。"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.tools import tool

from modules.p2p.repository import P2PRepository
from modules.p2p.tools._inject import _get_repository
from modules.p2p.tools._output import _clip_and_dump


# ── 收货异常分析 ────────────────────────────────────────────────────


def _analyze_receipt_anomalies_sync(
    repo: P2PRepository, supplier_id: str, po_number: str, days: int
) -> list[dict[str, Any]]:
    """同步执行收货异常分析。"""
    from datetime import date, timedelta
    from sqlalchemy import select
    from core.database.models import PoLine, PoLineLocation, RcvTransaction

    cutoff = date.today() - timedelta(days=days)
    anomalies: list[dict[str, Any]] = []

    with repo._session_factory() as session:
        stmt = select(RcvTransaction)
        if supplier_id:
            stmt = stmt.where(RcvTransaction.supplier_id == supplier_id)
        if po_number:
            stmt = stmt.where(RcvTransaction.po_number == po_number)
        stmt = stmt.where(RcvTransaction.transaction_date >= cutoff)
        receipts = session.scalars(stmt).all()

        for rcv in receipts:
            po_line = session.get(PoLine, rcv.po_line_id)
            if po_line is None:
                continue

            issues: list[str] = []

            # 超量收货
            if rcv.quantity > po_line.quantity:
                over_pct = ((rcv.quantity - po_line.quantity) / po_line.quantity) * 100
                issues.append(f"超量收货 {over_pct:.1f}%")

            # 拒收
            if rcv.rejected_quantity > 0:
                reject_pct = (rcv.rejected_quantity / rcv.quantity) * 100
                issues.append(f"拒收 {rcv.rejected_quantity} 件 ({reject_pct:.1f}%)")

            # 延迟收货
            loc = session.execute(
                select(PoLineLocation)
                .where(PoLineLocation.po_line_id == rcv.po_line_id)
                .limit(1)
            ).scalar()
            if loc and rcv.transaction_date > loc.promised_date:
                delay = (rcv.transaction_date - loc.promised_date).days
                issues.append(f"延迟 {delay} 天")

            if issues:
                anomalies.append({
                    "receipt_id": f"GR-{rcv.transaction_id:04d}",
                    "po_number": rcv.po_number,
                    "supplier_id": rcv.supplier_id,
                    "po_quantity": float(po_line.quantity),
                    "received_quantity": float(rcv.quantity),
                    "rejected_quantity": float(rcv.rejected_quantity),
                    "receipt_date": rcv.transaction_date.isoformat(),
                    "issues": issues,
                })

    return anomalies


@tool
async def analyze_receipt_anomalies(
    supplier_id: str = "",
    po_number: str = "",
    days: int = 30,
) -> str:
    """分析收货异常：超量收货、拒收、延迟收货。

    对比 PO 行数量与收货数量，检测超量收货（>PO 数量）、
    拒收（rejected_quantity > 0）、延迟收货（收货日期 > 承诺日期）。

    Args:
        supplier_id: 供应商 ID，空则分析全部。
        po_number: 采购订单号，空则分析全部。
        days: 分析最近 N 天。

    Returns:
        JSON 格式的收货异常列表。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(
        _analyze_receipt_anomalies_sync, repo, supplier_id, po_number, days
    )
    return _clip_and_dump(result)


# ── 重复发票检测 ────────────────────────────────────────────────────


def _detect_duplicate_invoices_sync(
    repo: P2PRepository, supplier_id: str, days: int
) -> list[dict[str, Any]]:
    """同步执行发票重复检测。"""
    from datetime import date, timedelta
    from sqlalchemy import select
    from core.database.models import ApInvoice

    cutoff = date.today() - timedelta(days=days)
    duplicates: list[dict[str, Any]] = []

    with repo._session_factory() as session:
        stmt = select(ApInvoice).where(ApInvoice.invoice_date >= cutoff)
        if supplier_id:
            stmt = stmt.where(ApInvoice.supplier_id == supplier_id)
        invoices = session.scalars(stmt).all()

        # 按 (supplier_id, invoice_amount) 分组
        groups: dict[tuple[str, float], list[Any]] = {}
        for inv in invoices:
            key = (inv.supplier_id, float(inv.invoice_amount))
            groups.setdefault(key, []).append(inv)

        for (sid, amount), group in groups.items():
            if len(group) < 2:
                continue
            # 检查日期接近度（±3天）
            group.sort(key=lambda x: x.invoice_date)
            for i in range(len(group) - 1):
                for j in range(i + 1, len(group)):
                    gap = abs((group[j].invoice_date - group[i].invoice_date).days)
                    if gap <= 3:
                        duplicates.append({
                            "supplier_id": sid,
                            "supplier_name": group[i].supplier_name,
                            "amount": amount,
                            "invoice_a": group[i].invoice_number,
                            "invoice_b": group[j].invoice_number,
                            "date_a": group[i].invoice_date.isoformat(),
                            "date_b": group[j].invoice_date.isoformat(),
                            "date_gap_days": gap,
                        })

    return duplicates


@tool
async def detect_duplicate_invoices(
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """检测重复发票：同供应商、同金额、同日期（±3天）的发票对。

    Args:
        supplier_id: 供应商 ID，空则检查全部。
        days: 分析最近 N 天。

    Returns:
        JSON 格式的疑似重复发票列表。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(
        _detect_duplicate_invoices_sync, repo, supplier_id, days
    )
    return _clip_and_dump(result)


# ── 折扣利用率分析 ──────────────────────────────────────────────────


def _analyze_discount_utilization_sync(
    repo: P2PRepository, supplier_id: str, days: int
) -> dict[str, Any]:
    """同步执行折扣利用率分析。"""
    from datetime import date, timedelta
    from sqlalchemy import select
    from core.database.models import ApInvoice, ApPayment

    cutoff = date.today() - timedelta(days=days)
    discount_rate = 0.02  # 假定标准 2% 折扣

    with repo._session_factory() as session:
        stmt = (
            select(ApInvoice)
            .where(
                ApInvoice.invoice_date >= cutoff,
                ApInvoice.discount_due_date.isnot(None),
            )
        )
        if supplier_id:
            stmt = stmt.where(ApInvoice.supplier_id == supplier_id)
        invoices = session.scalars(stmt).all()

        total_eligible = len(invoices)
        utilized = 0
        missed = 0
        missed_amount = 0.0
        details: list[dict[str, Any]] = []

        for inv in invoices:
            payment = session.execute(
                select(ApPayment)
                .where(ApPayment.invoice_number == inv.invoice_number)
                .limit(1)
            ).scalar()

            if payment is None:
                continue

            potential_saving = float(inv.invoice_amount) * discount_rate
            if payment.payment_date <= inv.discount_due_date:
                utilized += 1
            else:
                missed += 1
                missed_amount += potential_saving
                details.append({
                    "invoice_number": inv.invoice_number,
                    "supplier_name": inv.supplier_name,
                    "invoice_amount": float(inv.invoice_amount),
                    "discount_due_date": inv.discount_due_date.isoformat(),
                    "payment_date": payment.payment_date.isoformat(),
                    "days_late": (payment.payment_date - inv.discount_due_date).days,
                    "missed_saving": round(potential_saving, 2),
                })

        utilization_rate = (utilized / total_eligible * 100) if total_eligible > 0 else 0

    return {
        "total_eligible": total_eligible,
        "utilized": utilized,
        "missed": missed,
        "utilization_rate": round(utilization_rate, 1),
        "total_missed_saving": round(missed_amount, 2),
        "missed_details": details[:20],  # 最多返回 20 条
    }


@tool
async def analyze_discount_utilization(
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """分析早付折扣利用率：哪些发票有折扣机会但未利用。

    检查有 discount_due_date 的发票，对比实际付款日期，
    计算折扣利用率和因未利用折扣损失的金额。

    Args:
        supplier_id: 供应商 ID，空则分析全部。
        days: 分析最近 N 天。

    Returns:
        JSON 格式的折扣利用分析结果。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(
        _analyze_discount_utilization_sync, repo, supplier_id, days
    )
    return _clip_and_dump(result)


# ── 供应商集中度分析 ────────────────────────────────────────────────


def _analyze_vendor_concentration_sync(
    repo: P2PRepository, days: int, top_n: int
) -> dict[str, Any]:
    """同步执行供应商集中度分析。"""
    from datetime import date, timedelta
    from sqlalchemy import func, select
    from core.database.models import PoHeader, PoLine

    cutoff = date.today() - timedelta(days=days)

    with repo._session_factory() as session:
        # 按供应商汇总支出
        stmt = (
            select(
                PoHeader.supplier_id,
                PoHeader.supplier_name,
                func.sum(PoHeader.total_amount).label("total_spend"),
                func.count(func.distinct(PoHeader.po_number)).label("order_count"),
            )
            .where(PoHeader.creation_date >= cutoff)
            .group_by(PoHeader.supplier_id, PoHeader.supplier_name)
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
                "supplier_id": r.supplier_id,
                "supplier_name": r.supplier_name,
                "total_spend": round(spend, 2),
                "order_count": r.order_count,
                "spend_pct": round(pct, 1),
            }
            vendors.append(entry)
            if pct > 20:
                high_dependency.append(entry)

        # 单一来源品类
        cat_stmt = (
            select(
                PoLine.category,
                func.count(func.distinct(PoHeader.supplier_id)).label("vendor_count"),
            )
            .join(PoHeader, PoHeader.po_header_id == PoLine.po_header_id)
            .where(PoHeader.creation_date >= cutoff)
            .group_by(PoLine.category)
            .having(func.count(func.distinct(PoHeader.supplier_id)) == 1)
        )
        single_source = session.execute(cat_stmt).all()
        single_source_categories = [r.category for r in single_source]

    return {
        "grand_total_spend": round(grand_total, 2),
        "top_vendors": vendors,
        "high_dependency_vendors": high_dependency,
        "single_source_categories": single_source_categories,
    }


@tool
async def analyze_vendor_concentration(
    days: int = 30,
    top_n: int = 10,
) -> str:
    """分析供应商集中度：采购依赖度和单一来源风险。

    计算各供应商的采购占比，识别高依赖供应商（>20% 占比）
    和单一来源品类（某品类仅 1 家供应商）。

    Args:
        days: 分析最近 N 天。
        top_n: 返回 Top N 供应商。

    Returns:
        JSON 格式的供应商集中度分析结果。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(
        _analyze_vendor_concentration_sync, repo, days, top_n
    )
    return _clip_and_dump(result)


# ── PO 全流程周期 ───────────────────────────────────────────────────


def _calculate_po_cycle_time_sync(
    repo: P2PRepository, days: int, supplier_id: str
) -> dict[str, Any]:
    """同步执行 PO 周期分析。"""
    from datetime import date, timedelta
    from sqlalchemy import func, select
    from core.database.models import ApInvoice, ApPayment, PoHeader, RcvTransaction

    cutoff = date.today() - timedelta(days=days)

    with repo._session_factory() as session:
        stmt = select(PoHeader).where(PoHeader.creation_date >= cutoff)
        if supplier_id:
            stmt = stmt.where(PoHeader.supplier_id == supplier_id)
        orders = session.scalars(stmt).all()

        details: list[dict[str, Any]] = []
        total_create_to_receipt = 0
        total_receipt_to_payment = 0
        total_e2e = 0
        count_with_receipt = 0
        count_with_payment = 0

        for po in orders:
            entry: dict[str, Any] = {
                "po_number": po.po_number,
                "supplier_name": po.supplier_name,
                "created_date": po.creation_date.isoformat(),
            }

            # 最早收货日期
            first_receipt = session.execute(
                select(func.min(RcvTransaction.transaction_date))
                .where(RcvTransaction.po_number == po.po_number)
            ).scalar()

            if first_receipt:
                create_to_receipt = (first_receipt - po.creation_date).days
                entry["first_receipt_date"] = first_receipt.isoformat()
                entry["create_to_receipt_days"] = create_to_receipt
                total_create_to_receipt += create_to_receipt
                count_with_receipt += 1

                # 最早付款日期（通过发票关联）
                first_payment_date = session.execute(
                    select(func.min(ApPayment.payment_date))
                    .join(ApInvoice, ApPayment.invoice_number == ApInvoice.invoice_number)
                    .where(ApInvoice.po_number == po.po_number)
                ).scalar()

                if first_payment_date:
                    receipt_to_payment = (first_payment_date - first_receipt).days
                    e2e = (first_payment_date - po.creation_date).days
                    entry["first_payment_date"] = first_payment_date.isoformat()
                    entry["receipt_to_payment_days"] = receipt_to_payment
                    entry["end_to_end_days"] = e2e
                    total_receipt_to_payment += receipt_to_payment
                    total_e2e += e2e
                    count_with_payment += 1

            details.append(entry)

    return {
        "total_orders": len(details),
        "avg_create_to_receipt_days": (
            round(total_create_to_receipt / count_with_receipt, 1)
            if count_with_receipt > 0 else None
        ),
        "avg_receipt_to_payment_days": (
            round(total_receipt_to_payment / count_with_payment, 1)
            if count_with_payment > 0 else None
        ),
        "avg_end_to_end_days": (
            round(total_e2e / count_with_payment, 1)
            if count_with_payment > 0 else None
        ),
        "orders_with_receipt": count_with_receipt,
        "orders_with_payment": count_with_payment,
        "details": details[:30],
    }


@tool
async def calculate_po_cycle_time(
    days: int = 30,
    supplier_id: str = "",
) -> str:
    """计算采购订单全流程周期：创建→收货→付款各阶段耗时。

    Args:
        days: 分析最近 N 天的采购订单。
        supplier_id: 供应商 ID，空则分析全部。

    Returns:
        JSON 格式的周期分析结果。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(
        _calculate_po_cycle_time_sync, repo, days, supplier_id
    )
    return _clip_and_dump(result)
