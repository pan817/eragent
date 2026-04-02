"""
P2P Agent 工具集。

使用 LangChain @tool 装饰器定义结构化工具，供 P2P Agent 调用。
每个工具接受结构化参数，返回 JSON 字符串结果。

工具分为两类：
- query_* 工具：查询采购订单、收货、发票、付款等业务数据（MVP 阶段从模拟数据获取）
- run_* / calculate_* 工具：执行规则检查和 KPI 计算
"""

from __future__ import annotations

import json
from typing import Any

from langchain.tools import tool

from config.settings import get_settings
from modules.p2p.rules import (
    PaymentComplianceChecker,
    PriceVarianceAnalyzer,
    SupplierPerformanceCalculator,
    ThreeWayMatchChecker,
)


# ============================================================
# 模拟数据（基于 MockDataGenerator）
# ============================================================

_MOCK_CACHE: dict[str, Any] | None = None


def _get_mock_data() -> dict[str, Any]:
    """生成并缓存 MVP 阶段的模拟 P2P 业务数据。

    使用 MockDataGenerator 按 Oracle EBS 表结构生成数据，
    然后转换为规则引擎期望的扁平化格式。数据仅生成一次并缓存。

    Returns:
        包含 purchase_orders, receipts, invoices, payments, contract_prices 的字典。
    """
    global _MOCK_CACHE
    if _MOCK_CACHE is not None:
        return _MOCK_CACHE

    from modules.p2p.mock_data.generator import MockDataGenerator

    gen = MockDataGenerator(seed=0)
    raw = gen.generate_all()

    # --- 将 po_headers + po_lines 合并为规则引擎期望的扁平格式 ---
    line_map: dict[str, dict[str, Any]] = {
        pl["po_number"]: pl for pl in raw["po_lines"]
    }
    location_map: dict[str, dict[str, Any]] = {
        loc["po_number"]: loc for loc in raw["po_line_locations"]
    }
    purchase_orders: list[dict[str, Any]] = []
    contract_prices: dict[str, float] = {}
    for ph in raw["po_headers"]:
        pl = line_map.get(ph["po_number"], {})
        loc = location_map.get(ph["po_number"], {})
        purchase_orders.append({
            "po_number": ph["po_number"],
            "supplier_id": ph["supplier_id"],
            "supplier_name": ph["supplier_name"],
            "material_category": pl.get("category", ""),
            "po_amount": ph["total_amount"],
            "po_quantity": float(pl.get("quantity", 0)),
            "unit_price": float(pl.get("unit_price", 0)),
            "contract_price": float(pl.get("standard_price", 0)),
            "status": ph.get("status", "APPROVED").lower(),
            "creation_date": ph.get("creation_date", ""),
            "required_date": loc.get("promised_date", ""),
            "material_code": pl.get("item_code", ""),
            "material_name": pl.get("item_description", ""),
            "line_number": str(pl.get("line_num", "1")),
        })
        # 收集合同价
        item_code = pl.get("item_code", "")
        if item_code:
            contract_prices[item_code] = float(pl.get("standard_price", 0))

    # --- 收货数据转换 ---
    receipts: list[dict[str, Any]] = []
    for txn in raw["rcv_transactions"]:
        receipts.append({
            "receipt_id": f"GR-{txn['transaction_id']:04d}",
            "gr_number": f"GR-{txn['transaction_id']:04d}",
            "po_number": txn["po_number"],
            "supplier_id": txn.get("supplier_id", ""),
            "gr_quantity": float(txn["quantity"]),
            "receipt_date": txn["transaction_date"],
            "quality_passed": txn.get("rejected_quantity", 0) == 0,
        })

    # --- 发票数据转换 ---
    invoices: list[dict[str, Any]] = []
    for inv in raw["invoices"]:
        invoices.append({
            "invoice_number": inv["invoice_number"],
            "po_number": inv["po_number"],
            "supplier_id": inv["supplier_id"],
            "supplier_name": inv["supplier_name"],
            "invoice_amount": float(inv["invoice_amount"]),
            "due_date": inv["due_date"],
            "discount_due_date": inv.get("discount_due_date", ""),
            "discount_amount": float(inv["invoice_amount"]) * 0.98,
            "status": inv.get("status", "VALIDATED").lower(),
            "creation_date": inv.get("invoice_date", ""),
        })

    # --- 付款数据转换 ---
    payments: list[dict[str, Any]] = []
    for pmt in raw["payments"]:
        payments.append({
            "payment_id": pmt["payment_number"],
            "payment_number": pmt["payment_number"],
            "invoice_number": pmt["invoice_number"],
            "supplier_id": pmt["supplier_id"],
            "payment_amount": float(pmt["payment_amount"]),
            "payment_date": pmt["payment_date"],
            "payment_method": pmt.get("payment_method", "BANK_TRANSFER").lower(),
        })

    _MOCK_CACHE = {
        "purchase_orders": purchase_orders,
        "receipts": receipts,
        "invoices": invoices,
        "payments": payments,
        "contract_prices": contract_prices,
    }
    return _MOCK_CACHE


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
    MVP 阶段从模拟数据获取。

    Args:
        supplier_id: 供应商 ID，为空则返回全部。
        status: 订单状态过滤，如 approved、pending，为空则不过滤。
        days: 查询最近 N 天内的订单，默认 30 天。

    Returns:
        JSON 格式的采购订单列表字符串。
    """
    mock: dict[str, Any] = _get_mock_data()
    pos: list[dict[str, Any]] = mock["purchase_orders"]

    if supplier_id:
        pos = [po for po in pos if po.get("supplier_id") == supplier_id]
    if status:
        pos = [po for po in pos if po.get("status") == status]

    return json.dumps(pos, ensure_ascii=False, indent=2)


@tool
def query_receipts(
    po_number: str = "",
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """查询收货记录。

    根据采购订单号或供应商 ID 筛选收货记录。
    MVP 阶段从模拟数据获取。

    Args:
        po_number: 采购订单号，为空则不按 PO 过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        days: 查询最近 N 天内的记录，默认 30 天。

    Returns:
        JSON 格式的收货记录列表字符串。
    """
    mock: dict[str, Any] = _get_mock_data()
    receipts: list[dict[str, Any]] = mock["receipts"]

    if po_number:
        receipts = [r for r in receipts if r.get("po_number") == po_number]
    if supplier_id:
        receipts = [r for r in receipts if r.get("supplier_id") == supplier_id]

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
    MVP 阶段从模拟数据获取。

    Args:
        po_number: 采购订单号，为空则不按 PO 过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        status: 发票状态过滤，如 pending、paid，为空则不过滤。
        days: 查询最近 N 天内的发票，默认 30 天。

    Returns:
        JSON 格式的发票列表字符串。
    """
    mock: dict[str, Any] = _get_mock_data()
    invoices: list[dict[str, Any]] = mock["invoices"]

    if po_number:
        invoices = [inv for inv in invoices if inv.get("po_number") == po_number]
    if supplier_id:
        invoices = [inv for inv in invoices if inv.get("supplier_id") == supplier_id]
    if status:
        invoices = [inv for inv in invoices if inv.get("status") == status]

    return json.dumps(invoices, ensure_ascii=False, indent=2)


@tool
def query_payments(
    invoice_number: str = "",
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """查询付款记录。

    根据发票号或供应商 ID 筛选付款记录。
    MVP 阶段从模拟数据获取。

    Args:
        invoice_number: 发票号，为空则不按发票过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        days: 查询最近 N 天内的付款，默认 30 天。

    Returns:
        JSON 格式的付款记录列表字符串。
    """
    mock: dict[str, Any] = _get_mock_data()
    payments: list[dict[str, Any]] = mock["payments"]

    if invoice_number:
        payments = [p for p in payments if p.get("invoice_number") == invoice_number]
    if supplier_id:
        payments = [p for p in payments if p.get("supplier_id") == supplier_id]

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
    mock: dict[str, Any] = _get_mock_data()

    po_lines: list[dict[str, Any]] = mock["purchase_orders"]
    gr_lines: list[dict[str, Any]] = mock["receipts"]
    invoice_lines: list[dict[str, Any]] = mock["invoices"]

    if po_number:
        po_lines = [po for po in po_lines if po["po_number"] == po_number]
        gr_lines = [gr for gr in gr_lines if gr["po_number"] == po_number]
        invoice_lines = [inv for inv in invoice_lines if inv["po_number"] == po_number]

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
    mock: dict[str, Any] = _get_mock_data()

    po_lines: list[dict[str, Any]] = mock["purchase_orders"]
    contract_prices: dict[str, float] = mock["contract_prices"]

    if supplier_id:
        po_lines = [po for po in po_lines if po.get("supplier_id") == supplier_id]

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
    mock: dict[str, Any] = _get_mock_data()

    payments: list[dict[str, Any]] = mock["payments"]
    invoices: list[dict[str, Any]] = mock["invoices"]

    if supplier_id:
        payments = [p for p in payments if p.get("supplier_id") == supplier_id]
        invoices = [inv for inv in invoices if inv.get("supplier_id") == supplier_id]

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
    mock: dict[str, Any] = _get_mock_data()

    po_lines: list[dict[str, Any]] = [
        po for po in mock["purchase_orders"] if po.get("supplier_id") == supplier_id
    ]
    gr_lines: list[dict[str, Any]] = [
        gr for gr in mock["receipts"] if gr.get("supplier_id") == supplier_id
    ]
    invoices: list[dict[str, Any]] = [
        inv for inv in mock["invoices"] if inv.get("supplier_id") == supplier_id
    ]

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
