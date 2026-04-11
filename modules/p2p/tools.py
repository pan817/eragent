"""
P2P Agent 工具集。

使用 LangChain @tool 装饰器定义结构化工具，供 P2P Agent 调用。
每个工具接受结构化参数，返回 JSON 字符串结果。

工具分为两类：
- query_* 工具：查询采购订单、收货、发票、付款等业务数据（通过 PostgreSQL 查询）
- run_* / calculate_* 工具：执行规则检查和 KPI 计算
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain.tools import tool

from config.settings import get_settings
from core.database.repository import P2PRepository
from modules.p2p.rules import (
    PaymentComplianceChecker,
    PriceVarianceAnalyzer,
    SupplierPerformanceCalculator,
    ThreeWayMatchChecker,
)


# ============================================================
# Repository 访问（模块级变量，由外部注入）
# ============================================================

_repository: P2PRepository | None = None


def set_repository(repo: P2PRepository) -> None:
    """注入 P2PRepository 实例（服务启动时调用）。"""
    global _repository
    _repository = repo


def _get_repository() -> P2PRepository:
    """获取已注入的 Repository 实例。"""
    if _repository is None:
        raise RuntimeError(
            "P2PRepository 未初始化。请确保在服务启动时调用 set_repository()。"
        )
    return _repository


# ============================================================
# 查询工具
# ============================================================


@tool
async def query_purchase_orders(
    supplier_id: str = "",
    status: str = "",
    po_number: str = "",
    days: int = 30,
) -> str:
    """查询采购订单数据。

    根据供应商 ID、订单状态、采购订单号等条件筛选采购订单列表。

    Args:
        supplier_id: 供应商 ID，为空则返回全部。
        status: 订单状态过滤，如 approved、pending，为空则不过滤。
        po_number: 采购订单号，为空则不按 PO 过滤。
        days: 查询最近 N 天内的订单，默认 30 天。

    Returns:
        JSON 格式的采购订单列表字符串。
    """
    repo = _get_repository()
    pos = await asyncio.to_thread(
        repo.query_purchase_orders,
        supplier_id=supplier_id,
        status=status,
        po_number=po_number,
        days=days,
    )
    return json.dumps(pos, ensure_ascii=False, indent=2)


@tool
async def query_receipts(
    po_number: str = "",
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """查询收货记录。

    根据采购订单号或供应商 ID 筛选收货记录。

    Args:
        po_number: 采购订单号，为空则不按 PO 过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        days: 查询最近 N 天内的记录，默认 30 天。

    Returns:
        JSON 格式的收货记录列表字符串。
    """
    repo = _get_repository()
    receipts = await asyncio.to_thread(
        repo.query_receipts,
        po_number=po_number,
        supplier_id=supplier_id,
        days=days,
    )
    return json.dumps(receipts, ensure_ascii=False, indent=2)


@tool
async def query_invoices(
    po_number: str = "",
    supplier_id: str = "",
    status: str = "",
    invoice_number: str = "",
    days: int = 30,
) -> str:
    """查询发票数据。

    根据采购订单号、供应商 ID、发票号或发票状态筛选发票记录。

    Args:
        po_number: 采购订单号，为空则不按 PO 过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        status: 发票状态过滤，如 pending、paid，为空则不过滤。
        invoice_number: 发票号，为空则不按发票过滤。
        days: 查询最近 N 天内的发票，默认 30 天。

    Returns:
        JSON 格式的发票列表字符串。
    """
    repo = _get_repository()
    invoices = await asyncio.to_thread(
        repo.query_invoices,
        po_number=po_number,
        supplier_id=supplier_id,
        status=status,
        invoice_number=invoice_number,
        days=days,
    )
    return json.dumps(invoices, ensure_ascii=False, indent=2)


@tool
async def query_payments(
    invoice_number: str = "",
    supplier_id: str = "",
    payment_number: str = "",
    days: int = 30,
) -> str:
    """查询付款记录。

    根据发票号、供应商 ID 或付款单号筛选付款记录。

    Args:
        invoice_number: 发票号，为空则不按发票过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        payment_number: 付款单号，为空则不按付款单过滤。
        days: 查询最近 N 天内的付款，默认 30 天。

    Returns:
        JSON 格式的付款记录列表字符串。
    """
    repo = _get_repository()
    payments = await asyncio.to_thread(
        repo.query_payments,
        invoice_number=invoice_number,
        supplier_id=supplier_id,
        payment_number=payment_number,
        days=days,
    )
    return json.dumps(payments, ensure_ascii=False, indent=2)


# ============================================================
# 分析工具
# ============================================================


def _run_three_way_match_sync(po_number: str) -> str:
    """同步执行三路匹配的真实实现，供 async tool 通过线程池调用。"""
    settings = get_settings()
    repo = _get_repository()

    po_lines = repo.get_flattened_purchase_orders(po_number=po_number)
    gr_lines = repo.get_flattened_receipts(po_number=po_number)
    invoice_lines = repo.get_flattened_invoices(po_number=po_number)

    checker = ThreeWayMatchChecker(settings.p2p)
    anomalies = checker.check(po_lines, gr_lines, invoice_lines)

    result: list[dict[str, Any]] = [
        anomaly.model_dump(mode="json") for anomaly in anomalies
    ]
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@tool
async def run_three_way_match(po_number: str = "") -> str:
    """执行三路匹配检查，比对采购订单、收货单、发票的金额和数量。

    当偏差超过配置容差时返回异常记录。若指定 po_number 则只检查该订单，
    否则检查所有订单。

    Args:
        po_number: 采购订单号，为空则检查所有订单。

    Returns:
        JSON 格式的异常列表字符串，每条包含异常类型、严重等级、偏差详情。
    """
    return await asyncio.to_thread(_run_three_way_match_sync, po_number)


def _run_price_variance_analysis_sync(supplier_id: str, days: int) -> str:
    """同步执行价格差异分析的真实实现。"""
    settings = get_settings()
    repo = _get_repository()

    po_lines = repo.get_flattened_purchase_orders(supplier_id=supplier_id)
    contract_prices = repo.get_contract_prices()

    analyzer = PriceVarianceAnalyzer(settings.p2p)
    anomalies = analyzer.analyze(po_lines, contract_prices)

    result: list[dict[str, Any]] = [
        anomaly.model_dump(mode="json") for anomaly in anomalies
    ]
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


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


def _run_payment_compliance_check_sync(supplier_id: str, days: int) -> str:
    """同步执行付款合规检查的真实实现。"""
    settings = get_settings()
    repo = _get_repository()

    payments = repo.get_flattened_payments(supplier_id=supplier_id)
    invoices = repo.get_flattened_invoices(supplier_id=supplier_id)

    checker = PaymentComplianceChecker(settings.p2p)
    anomalies = checker.check(payments, invoices)

    result: list[dict[str, Any]] = [
        anomaly.model_dump(mode="json") for anomaly in anomalies
    ]
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@tool
async def run_payment_compliance_check(
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """执行付款合规性检查，检测逾期付款、提前付款和折扣滥用。

    将付款数据与发票数据关联，检查付款日期是否符合合同约定。

    Args:
        supplier_id: 供应商 ID，为空则检查所有供应商。
        days: 检查最近 N 天内的付款，默认 30 天。

    Returns:
        JSON 格式的付款合规性异常列表字符串。
    """
    return await asyncio.to_thread(
        _run_payment_compliance_check_sync, supplier_id, days
    )


def _calculate_supplier_kpis_sync(supplier_id: str, period: str) -> str:
    """同步计算供应商 KPI 的真实实现。"""
    settings = get_settings()
    repo = _get_repository()

    po_lines = repo.get_flattened_purchase_orders(supplier_id=supplier_id)
    gr_lines = repo.get_flattened_receipts(supplier_id=supplier_id)
    invoices = repo.get_flattened_invoices(supplier_id=supplier_id)

    # 获取供应商名称
    supplier_name: str = ""
    if po_lines:
        supplier_name = po_lines[0].get("supplier_name", supplier_id)

    if not period:
        period = "近30天"

    calculator = SupplierPerformanceCalculator(settings.p2p)
    report = calculator.calculate(
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        po_lines=po_lines,
        gr_lines=gr_lines,
        invoices=invoices,
        period=period,
    )

    return json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)


@tool
async def calculate_supplier_kpis(
    supplier_id: str,
    period: str = "",
) -> str:
    """计算供应商绩效 KPI，包括准时交付率、发票准确率、质检合格率、价格合规率。

    基于采购订单、收货、发票数据综合计算四项核心 KPI 指标，
    并与配置基准值比较生成状态评级。

    Args:
        supplier_id: 供应商 ID（必填）。
        period: 评估周期描述，如 "2026-Q1"，为空则使用 "近30天"。

    Returns:
        JSON 格式的供应商 KPI 报告字符串。
    """
    return await asyncio.to_thread(_calculate_supplier_kpis_sync, supplier_id, period)


# ============================================================
# 新增工具（Phase 2）
# ============================================================

_tool_logger: Any = None


def _get_tool_logger() -> Any:
    """延迟获取 logger，避免模块级循环导入。"""
    global _tool_logger
    if _tool_logger is None:
        from core.logging_utils import get_logger
        _tool_logger = get_logger(__name__)
    return _tool_logger


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
    return json.dumps(suppliers, ensure_ascii=False, indent=2)


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
    return json.dumps(result, ensure_ascii=False, indent=2)


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


# ── 存根工具（Phase 4 真实实现） ─────────────────────────────────────


@tool
async def query_material_master(material_ids: str = "") -> str:
    """查询物料主数据信息（存根，Phase 4 实现）。

    Args:
        material_ids: 逗号分隔的物料 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("query_material_master is a stub, returning empty result")
    return "[]"


@tool
async def calculate_po_cycle_time(days: int = 30) -> str:
    """计算采购订单周期时间（存根，Phase 4 实现）。

    Args:
        days: 分析最近 N 天内的数据。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("calculate_po_cycle_time is a stub, returning empty result")
    return "[]"


@tool
async def run_vendor_risk_scoring(vendor_ids: str = "") -> str:
    """执行供应商风险评分（存根，Phase 4 实现）。

    Args:
        vendor_ids: 逗号分隔的供应商 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("run_vendor_risk_scoring is a stub, returning empty result")
    return "[]"


@tool
async def check_approval_limits(po_ids: str = "") -> str:
    """检查采购审批限额合规性（存根，Phase 4 实现）。

    Args:
        po_ids: 逗号分隔的采购订单 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("check_approval_limits is a stub, returning empty result")
    return "[]"


@tool
async def check_blacklist(vendor_ids: str = "") -> str:
    """检查供应商黑名单（存根，Phase 4 实现）。

    Args:
        vendor_ids: 逗号分隔的供应商 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("check_blacklist is a stub, returning empty result")
    return "[]"
