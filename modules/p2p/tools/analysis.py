"""P2P 规则分析工具（6 个）。"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.tools import tool

from config.settings import get_settings
from modules.p2p.repository import P2PRepository
from modules.p2p.tools._inject import _get_repository
from modules.p2p.tools._output import _clip_and_dump


# ── 三路匹配 ────────────────────────────────────────────────────────


def _run_three_way_match_sync(po_number: str) -> str:
    """同步执行三路匹配检查的真实实现。"""
    from modules.p2p.settings import get_p2p_settings
    from modules.p2p.rules import ThreeWayMatchChecker

    repo = _get_repository()

    po_lines = repo.get_flattened_purchase_orders(po_number=po_number)
    gr_lines = repo.get_flattened_receipts(po_number=po_number)
    invoice_lines = repo.get_flattened_invoices(po_number=po_number)

    checker = ThreeWayMatchChecker(get_p2p_settings())
    anomalies = checker.check(po_lines, gr_lines, invoice_lines)

    result: list[dict[str, Any]] = [
        anomaly.model_dump(mode="json") for anomaly in anomalies
    ]
    return _clip_and_dump(result)


@tool
async def run_three_way_match(po_number: str = "") -> str:
    """执行三路匹配检查，比对 PO/收货/发票的数量和金额。

    偏差超出容差阈值（默认 5%）时返回异常记录。

    Args:
        po_number: 采购订单号，为空则检查所有订单。

    Returns:
        JSON 格式的三路匹配异常列表字符串。
    """
    return await asyncio.to_thread(_run_three_way_match_sync, po_number)


# ── 价格差异 ────────────────────────────────────────────────────────


def _run_price_variance_analysis_sync(supplier_id: str, days: int) -> str:
    """同步执行价格差异分析的真实实现。"""
    from modules.p2p.settings import get_p2p_settings
    from modules.p2p.rules import PriceVarianceAnalyzer

    repo = _get_repository()

    po_lines = repo.get_flattened_purchase_orders(supplier_id=supplier_id)
    contract_prices = repo.get_contract_prices()

    analyzer = PriceVarianceAnalyzer(get_p2p_settings())
    anomalies = analyzer.analyze(po_lines, contract_prices)

    result: list[dict[str, Any]] = [
        anomaly.model_dump(mode="json") for anomaly in anomalies
    ]
    return _clip_and_dump(result)


@tool
async def run_price_variance_analysis(
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """执行价格差异分析，比对实际采购单价与合同价/标准价。

    偏差超出容差阈值时返回异常记录。

    Args:
        supplier_id: 供应商 ID，为空则分析所有供应商。
        days: 分析最近 N 天内的采购订单，默认 30 天。

    Returns:
        JSON 格式的价格差异异常列表字符串。
    """
    return await asyncio.to_thread(
        _run_price_variance_analysis_sync, supplier_id, days
    )


# ── 付款合规 ────────────────────────────────────────────────────────


def _run_payment_compliance_check_sync(supplier_id: str, days: int) -> str:
    """同步执行付款合规检查的真实实现。"""
    from modules.p2p.settings import get_p2p_settings
    from modules.p2p.rules import PaymentComplianceChecker

    repo = _get_repository()

    payments = repo.get_flattened_payments(supplier_id=supplier_id)
    invoices = repo.get_flattened_invoices(supplier_id=supplier_id)

    checker = PaymentComplianceChecker(get_p2p_settings())
    anomalies = checker.check(payments, invoices)

    result: list[dict[str, Any]] = [
        anomaly.model_dump(mode="json") for anomaly in anomalies
    ]
    return _clip_and_dump(result)


@tool
async def run_payment_compliance_check(
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """执行付款合规检查，检测超期付款、提前付款和折扣滥用。

    Args:
        supplier_id: 供应商 ID，为空则检查所有供应商。
        days: 分析最近 N 天内的付款，默认 30 天。

    Returns:
        JSON 格式的付款合规异常列表字符串。
    """
    return await asyncio.to_thread(
        _run_payment_compliance_check_sync, supplier_id, days
    )


# ── 供应商绩效 ──────────────────────────────────────────────────────


def _calculate_supplier_kpis_sync(supplier_id: str, period: str) -> str:
    """同步执行供应商 KPI 计算的真实实现。"""
    from modules.p2p.settings import get_p2p_settings
    from modules.p2p.rules import SupplierPerformanceCalculator

    settings = get_settings()
    repo = _get_repository()

    po_lines = repo.get_flattened_purchase_orders(supplier_id=supplier_id)
    gr_lines = repo.get_flattened_receipts(supplier_id=supplier_id)
    invoices = repo.get_flattened_invoices(supplier_id=supplier_id)

    supplier_name: str = ""
    if po_lines:
        supplier_name = po_lines[0].get("supplier_name", supplier_id)

    if not period:
        period = "近30天"

    calculator = SupplierPerformanceCalculator(get_p2p_settings())
    report = calculator.calculate(
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        po_lines=po_lines,
        gr_lines=gr_lines,
        invoices=invoices,
        period=period,
    )

    return _clip_and_dump(report.model_dump(mode="json"))


@tool
async def calculate_supplier_kpis(
    supplier_id: str = "",
    period: str = "",
) -> str:
    """计算供应商绩效 KPI：按时交付率、发票准确率、质量合格率、价格合规率。

    Args:
        supplier_id: 供应商 ID，为空则计算所有供应商汇总。
        period: 分析周期描述，默认"近30天"。

    Returns:
        JSON 格式的供应商 KPI 报告字符串。
    """
    return await asyncio.to_thread(
        _calculate_supplier_kpis_sync, supplier_id, period
    )


# ── 供应商主数据查询 ────────────────────────────────────────────────


def _query_vendor_master_sync(repo: P2PRepository, vendor_ids: list[str]) -> list[dict[str, Any]]:
    """同步查询供应商主数据。"""
    from sqlalchemy import select
    from core.database.models import ApSupplier

    with repo._session_factory() as session:
        stmt = select(
            ApSupplier.supplier_id,
            ApSupplier.supplier_name,
            ApSupplier.supplier_site_id,
            ApSupplier.payment_terms,
            ApSupplier.status,
        )
        if vendor_ids:
            stmt = stmt.where(ApSupplier.supplier_id.in_(vendor_ids))

        rows = session.execute(stmt).all()
        return [
            {
                "supplier_id": r.supplier_id,
                "supplier_name": r.supplier_name,
                "supplier_site_id": r.supplier_site_id,
                "payment_terms": r.payment_terms,
                "status": r.status,
            }
            for r in rows
        ]


@tool
async def query_vendor_master(vendor_ids: str = "") -> str:
    """查询供应商主数据信息。

    返回供应商基本信息，包括名称、站点、付款条款、状态等。

    Args:
        vendor_ids: 逗号分隔的供应商 ID 列表，为空则返回全部。

    Returns:
        JSON 格式的供应商主数据列表字符串。
    """
    repo = _get_repository()
    ids = [v.strip() for v in vendor_ids.split(",") if v.strip()] if vendor_ids else []
    suppliers = await asyncio.to_thread(_query_vendor_master_sync, repo, ids)
    return _clip_and_dump(suppliers)


# ── 支出分析 ────────────────────────────────────────────────────────


def _calculate_spend_analysis_sync(
    repo: P2PRepository, group_by: str, days: int
) -> list[dict[str, Any]]:
    """同步执行采购支出聚合分析。"""
    from datetime import date, timedelta
    from sqlalchemy import func, select
    from core.database.models import PoHeader, PoLine

    cutoff = date.today() - timedelta(days=days)

    with repo._session_factory() as session:
        if group_by == "supplier":
            group_col = PoHeader.supplier_name
        else:
            group_col = PoLine.category

        stmt = (
            select(
                group_col.label("group_key"),
                func.count(func.distinct(PoHeader.po_number)).label("order_count"),
                func.sum(PoLine.amount).label("total_amount"),
                func.avg(PoLine.unit_price).label("avg_unit_price"),
            )
            .join(PoLine, PoHeader.po_header_id == PoLine.po_header_id)
            .where(PoHeader.creation_date >= cutoff)
            .group_by(group_col)
            .order_by(func.sum(PoLine.amount).desc())
        )

        rows = session.execute(stmt).all()
        return [
            {
                "group_by": group_by,
                "group_key": str(r.group_key),
                "order_count": r.order_count,
                "total_amount": float(r.total_amount) if r.total_amount else 0.0,
                "avg_unit_price": round(float(r.avg_unit_price), 2) if r.avg_unit_price else 0.0,
            }
            for r in rows
        ]


@tool
async def calculate_spend_analysis(
    group_by: str = "category",
    days: int = 30,
) -> str:
    """按维度聚合采购支出分析。

    对采购订单金额按指定维度（供应商或品类）进行汇总统计。

    Args:
        group_by: 聚合维度，"category"（按品类）或 "supplier"（按供应商），默认 category。
        days: 分析最近 N 天内的数据，默认 30 天。

    Returns:
        JSON 格式的支出分析结果字符串。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(_calculate_spend_analysis_sync, repo, group_by, days)
    return _clip_and_dump(result)
