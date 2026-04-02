"""
P2P Agent 工具集。

使用 LangChain @tool 装饰器定义结构化工具，供 P2P Agent 调用。
每个工具接受结构化参数，返回 JSON 字符串结果。

工具分为两类：
- query_* 工具：查询采购订单、收货、发票、付款等业务数据（通过 PostgreSQL 查询）
- run_* / calculate_* 工具：执行规则检查和 KPI 计算
"""

from __future__ import annotations

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
def query_purchase_orders(
    supplier_id: str = "",
    status: str = "",
    days: int = 30,
) -> str:
    """查询采购订单数据。

    根据供应商 ID、订单状态等条件筛选采购订单列表。

    Args:
        supplier_id: 供应商 ID，为空则返回全部。
        status: 订单状态过滤，如 approved、pending，为空则不过滤。
        days: 查询最近 N 天内的订单，默认 30 天。

    Returns:
        JSON 格式的采购订单列表字符串。
    """
    repo = _get_repository()
    pos = repo.query_purchase_orders(supplier_id=supplier_id, status=status, days=days)
    return json.dumps(pos, ensure_ascii=False, indent=2)


@tool
def query_receipts(
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
    receipts = repo.query_receipts(po_number=po_number, supplier_id=supplier_id, days=days)
    return json.dumps(receipts, ensure_ascii=False, indent=2)


@tool
def query_invoices(
    po_number: str = "",
    supplier_id: str = "",
    status: str = "",
    days: int = 30,
) -> str:
    """查询发票数据。

    根据采购订单号、供应商 ID 或发票状态筛选发票记录。

    Args:
        po_number: 采购订单号，为空则不按 PO 过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        status: 发票状态过滤，如 pending、paid，为空则不过滤。
        days: 查询最近 N 天内的发票，默认 30 天。

    Returns:
        JSON 格式的发票列表字符串。
    """
    repo = _get_repository()
    invoices = repo.query_invoices(
        po_number=po_number, supplier_id=supplier_id, status=status, days=days
    )
    return json.dumps(invoices, ensure_ascii=False, indent=2)


@tool
def query_payments(
    invoice_number: str = "",
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """查询付款记录。

    根据发票号或供应商 ID 筛选付款记录。

    Args:
        invoice_number: 发票号，为空则不按发票过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        days: 查询最近 N 天内的付款，默认 30 天。

    Returns:
        JSON 格式的付款记录列表字符串。
    """
    repo = _get_repository()
    payments = repo.query_payments(
        invoice_number=invoice_number, supplier_id=supplier_id, days=days
    )
    return json.dumps(payments, ensure_ascii=False, indent=2)


# ============================================================
# 分析工具
# ============================================================


@tool
def run_three_way_match(po_number: str = "") -> str:
    """执行三路匹配检查，比对采购订单、收货单、发票的金额和数量。

    当偏差超过配置容差时返回异常记录。若指定 po_number 则只检查该订单，
    否则检查所有订单。

    Args:
        po_number: 采购订单号，为空则检查所有订单。

    Returns:
        JSON 格式的异常列表字符串，每条包含异常类型、严重等级、偏差详情。
    """
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
def run_price_variance_analysis(
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
def run_payment_compliance_check(
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
def calculate_supplier_kpis(
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
