"""PG 规则检测工具（6个）。

通过 QueryBackend 获取数据后喂入规则引擎。
postgresql 模式 → SQL 查询；hybrid 模式 → Neo4j 节点查询。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.tools import tool

from modules.p2p.tools._inject import _get_query_backend, _get_repository
from modules.p2p.tools._output import _clip_and_dump


# ── 三路匹配 ────────────────────────────────────────────────────────


async def _run_three_way_match_impl(po_number: str) -> str:
    from config.settings import get_settings
    from modules.p2p.settings import get_p2p_settings
    from modules.p2p.rules import ThreeWayMatchChecker

    mode = get_settings().graphiti_etl.query_backend
    backend = _get_query_backend()

    if mode != "postgresql":
        # hybrid 模式：利用边精确匹配 PO行↔收货↔发票行
        matched_data = await _three_way_match_graph_enhanced(po_number)
        if matched_data is not None:
            checker = ThreeWayMatchChecker(get_p2p_settings())
            anomalies = checker.check(
                matched_data["po_lines"],
                matched_data["gr_lines"],
                matched_data["invoice_lines"],
            )
            result: list[dict[str, Any]] = [
                anomaly.model_dump(mode="json") for anomaly in anomalies
            ]
            return _clip_and_dump(result)

    # postgresql 模式或图查询失败时走字段匹配
    po_lines = await backend.query_purchase_orders(po_number=po_number, days=0)
    gr_lines = await backend.query_receipts(po_number=po_number, days=0)
    invoice_lines = await backend.query_invoices(po_number=po_number, days=0)

    checker = ThreeWayMatchChecker(get_p2p_settings())
    anomalies = checker.check(po_lines, gr_lines, invoice_lines)

    result = [anomaly.model_dump(mode="json") for anomaly in anomalies]
    return _clip_and_dump(result)


async def _three_way_match_graph_enhanced(po_number: str) -> dict[str, Any] | None:
    """通过图边遍历精确匹配 PO行↔收货↔发票行。"""
    from modules.p2p.tools._inject import _get_graphiti_client

    try:
        client = _get_graphiti_client()
    except RuntimeError:
        return None

    cypher = """
    MATCH (po:Entity {entity_type:'PurchaseOrder'})-[:RELATES_TO {name:'CONTAINS_LINE'}]->(line:Entity)
    WHERE ($po_number IS NULL OR po.po_number = $po_number)
    OPTIONAL MATCH (line)<-[:RELATES_TO {name:'RECEIVES_LINE'}]-(rcv:Entity)
    OPTIONAL MATCH (line)<-[:RELATES_TO {name:'INVOICES_LINE'}]-(il:Entity)
    RETURN po.po_number AS po_number, po.vendor_id AS vendor_id,
           po.vendor_name AS vendor_name,
           line.entity_id AS line_id, line.quantity AS po_quantity,
           line.unit_price AS unit_price, line.standard_price AS contract_price,
           line.category_id AS material_category,
           line.item_id AS material_code, line.item_description AS material_name,
           line.line_num AS line_number,
           rcv.quantity AS rcv_quantity, rcv.valid_from AS receipt_date,
           il.quantity_invoiced AS inv_quantity, il.unit_price AS inv_unit_price
    """
    params = {"po_number": po_number if po_number else None}
    result = await client.execute_cypher(cypher, params)

    if not result:
        return None

    records = result if isinstance(result, list) else (
        [dict(r) for r in result.records] if hasattr(result, "records") else []
    )
    if not records:
        return None

    def _sf(v: Any) -> float:
        try:
            return float(v) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    po_lines = []
    gr_lines = []
    invoice_lines = []
    for r in records:
        po_lines.append({
            "po_number": r.get("po_number", ""),
            "vendor_id": r.get("vendor_id", ""),
            "vendor_name": r.get("vendor_name", ""),
            "material_category": r.get("material_category", ""),
            "po_amount": _sf(r.get("po_quantity")) * _sf(r.get("unit_price")),
            "po_quantity": _sf(r.get("po_quantity")),
            "unit_price": _sf(r.get("unit_price")),
            "contract_price": _sf(r.get("contract_price")),
            "material_code": r.get("material_code", ""),
            "material_name": r.get("material_name", ""),
            "line_number": str(r.get("line_number", "1")),
        })
        if r.get("rcv_quantity") is not None:
            gr_lines.append({
                "po_number": r.get("po_number", ""),
                "vendor_id": r.get("vendor_id", ""),
                "gr_quantity": _sf(r.get("rcv_quantity")),
                "receipt_date": str(r.get("receipt_date", "")),
            })
        if r.get("inv_quantity") is not None:
            invoice_lines.append({
                "po_number": r.get("po_number", ""),
                "vendor_id": r.get("vendor_id", ""),
                "invoice_amount": _sf(r.get("inv_quantity")) * _sf(r.get("inv_unit_price")),
            })

    return {"po_lines": po_lines, "gr_lines": gr_lines, "invoice_lines": invoice_lines}


@tool
async def run_three_way_match(po_number: str = "") -> str:
    """执行三路匹配检查，比对 PO/收货/发票的数量和金额偏差。

    适用场景：需要检查采购订单的数量和金额是否在 PO、收货、发票三方之间一致。
    返回内容：偏差超出容差阈值（默认 5%）的异常记录列表，含偏差类型和百分比。
    不适用：检测流程缺失（无收货/无发票）请用 detect_graph_anomalies。

    Args:
        po_number: 采购订单号，为空则检查所有订单。

    Returns:
        JSON 格式的三路匹配异常列表字符串。
    """
    return await _run_three_way_match_impl(po_number)


# ── 价格差异 ────────────────────────────────────────────────────────


async def _run_price_variance_analysis_impl(vendor_id: str, days: int) -> str:
    from modules.p2p.settings import get_p2p_settings
    from modules.p2p.rules import PriceVarianceAnalyzer

    backend = _get_query_backend()

    po_lines = await backend.query_purchase_orders(vendor_id=vendor_id, days=0)
    contract_prices = await backend.get_contract_prices()

    analyzer = PriceVarianceAnalyzer(get_p2p_settings())
    anomalies = analyzer.analyze(po_lines, contract_prices)

    result: list[dict[str, Any]] = [
        anomaly.model_dump(mode="json") for anomaly in anomalies
    ]
    return _clip_and_dump(result)


@tool
async def run_price_variance_analysis(
    vendor_id: str = "",
    days: int = 30,
) -> str:
    """执行价格差异分析，比对实际采购单价与合同价/标准价。

    适用场景：需要检查采购订单的实际单价是否偏离合同约定价格。
    返回内容：偏差超出容差阈值的异常记录，含实际价格、合同价格和偏差百分比。
    不适用：查看合同覆盖情况请用 find_contract_coverage。

    Args:
        vendor_id: 供应商 ID，为空则分析所有供应商。
        days: 分析最近 N 天内的采购订单，默认 30 天。

    Returns:
        JSON 格式的价格差异异常列表字符串。
    """
    return await _run_price_variance_analysis_impl(vendor_id, days)


# ── 付款合规 ────────────────────────────────────────────────────────


async def _run_payment_compliance_check_impl(vendor_id: str, days: int) -> str:
    from modules.p2p.settings import get_p2p_settings
    from modules.p2p.rules import PaymentComplianceChecker

    backend = _get_query_backend()

    payments = await backend.query_payments(vendor_id=vendor_id, days=0)
    invoices = await backend.query_invoices(vendor_id=vendor_id, days=0)

    checker = PaymentComplianceChecker(get_p2p_settings())
    anomalies = checker.check(payments, invoices)

    result: list[dict[str, Any]] = [
        anomaly.model_dump(mode="json") for anomaly in anomalies
    ]
    return _clip_and_dump(result)


@tool
async def run_payment_compliance_check(
    vendor_id: str = "",
    days: int = 30,
) -> str:
    """执行付款合规检查，检测超期付款、提前付款和折扣滥用。

    适用场景：需要检查付款是否在规定期限内完成、是否有违规提前付款或折扣滥用。
    返回内容：合规异常列表，含异常类型（超期/提前/折扣滥用）和违规天数。
    不适用：分析折扣利用率请用 analyze_discount_utilization；检测未授权付款请用 detect_graph_anomalies(scope="unauthorized_payment")。

    Args:
        vendor_id: 供应商 ID，为空则检查所有供应商。
        days: 分析最近 N 天内的付款，默认 30 天。

    Returns:
        JSON 格式的付款合规异常列表字符串。
    """
    return await _run_payment_compliance_check_impl(vendor_id, days)


# ── 供应商绩效 ──────────────────────────────────────────────────────


async def _calculate_supplier_kpis_impl(vendor_id: str, period: str) -> str:
    from modules.p2p.settings import get_p2p_settings
    from modules.p2p.rules import SupplierPerformanceCalculator

    backend = _get_query_backend()

    if not period:
        period = "近30天"

    calculator = SupplierPerformanceCalculator(get_p2p_settings())

    if vendor_id:
        report = await _calc_single_supplier_kpis(
            backend, calculator, vendor_id, period,
        )
        return _clip_and_dump(report.model_dump(mode="json"))

    # vendor_id 为空 → 遍历所有供应商
    all_po = await backend.query_purchase_orders(days=0)
    vendor_map: dict[str, str] = {}
    for po in all_po:
        sid = po.get("vendor_id", "")
        if sid and sid not in vendor_map:
            vendor_map[sid] = po.get("vendor_name", sid)

    if not vendor_map:
        return _clip_and_dump([])

    reports = []
    for sid in vendor_map:
        report = await _calc_single_supplier_kpis(backend, calculator, sid, period)
        reports.append(report.model_dump(mode="json"))
    return _clip_and_dump(reports)


async def _calc_single_supplier_kpis(
    backend: Any,
    calculator: Any,
    vendor_id: str,
    period: str,
) -> Any:
    from api.schemas.domain import SupplierKPIReport

    po_lines = await backend.query_purchase_orders(vendor_id=vendor_id, days=0)
    gr_lines = await backend.query_receipts(vendor_id=vendor_id, days=0)
    invoices = await backend.query_invoices(vendor_id=vendor_id, days=0)

    vendor_name = ""
    if po_lines:
        vendor_name = po_lines[0].get("vendor_name", vendor_id)

    return calculator.calculate(
        vendor_id=vendor_id,
        vendor_name=vendor_name,
        po_lines=po_lines,
        gr_lines=gr_lines,
        invoices=invoices,
        period=period,
    )


@tool
async def calculate_supplier_kpis(
    vendor_id: str = "",
    period: str = "",
) -> str:
    """计算供应商绩效 KPI：按时交付率、发票准确率、质量合格率、价格合规率。

    适用场景：需要量化评估供应商的交付和合规表现。
    返回内容：KPI 报告（单个或列表），含各项指标的百分比和评级。
    不适用：查看供应商全景关系画像请用 query_supplier_profile；对比多供应商请用 compare_entities。

    Args:
        vendor_id: 供应商 ID，为空则逐个计算所有供应商并返回列表。
        period: 分析周期描述（如"近30天"），默认"近30天"。

    Returns:
        JSON 格式的供应商 KPI 报告。
    """
    return await _calculate_supplier_kpis_impl(vendor_id, period)


# ── 供应商主数据查询 ────────────────────────────────────────────────


async def _query_vendor_master_impl(vendor_ids: list[str]) -> list[dict[str, Any]]:
    backend = _get_query_backend()

    if vendor_ids:
        results: list[dict[str, Any]] = []
        for vid in vendor_ids:
            suppliers = await backend.query_suppliers(vendor_id=vid)
            results.extend(suppliers)
        return results
    else:
        return await backend.query_suppliers()


@tool
async def query_vendor_master(vendor_ids: str = "") -> str:
    """查询供应商主数据信息（名称、站点、付款条款、状态等）。

    适用场景：需要了解供应商的基本信息和资质状态。
    返回内容：供应商主数据列表（名称、站点、付款条款、启用状态）。
    不适用：查看供应商绩效请用 calculate_supplier_kpis；查看供应商关系网络请用 query_supplier_profile。

    Args:
        vendor_ids: 逗号分隔的供应商 ID 列表（如 "V001,V002"），为空则返回全部。

    Returns:
        JSON 格式的供应商主数据列表字符串。
    """
    ids = [v.strip() for v in vendor_ids.split(",") if v.strip()] if vendor_ids else []
    suppliers = await _query_vendor_master_impl(ids)
    return _clip_and_dump(suppliers)


# ── 支出分析 ────────────────────────────────────────────────────────


async def _calculate_spend_analysis_impl(group_by: str, days: int) -> list[dict[str, Any]]:
    """采购支出聚合分析。

    postgresql 模式：直接 SQL GROUP BY（性能最优）。
    hybrid 模式：QueryBackend 取数据 + Python 聚合。
    """
    from config.settings import get_settings

    mode = get_settings().graphiti_etl.query_backend

    if mode == "postgresql":
        repo = _get_repository()
        return await asyncio.to_thread(_spend_analysis_sql, repo, group_by, days)
    else:
        backend = _get_query_backend()
        pos = await backend.query_purchase_orders(days=days)
        return _spend_analysis_python(pos, group_by)


def _spend_analysis_sql(repo: Any, group_by: str, days: int) -> list[dict[str, Any]]:
    """SQL 聚合实现。"""
    from datetime import date, timedelta
    from sqlalchemy import func, select
    from core.database.models import PoHeader, PoLine

    cutoff = date.today() - timedelta(days=days)

    with repo._session_factory() as session:
        if group_by == "supplier":
            group_col = PoHeader.vendor_name
        else:
            group_col = PoLine.category_id

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


def _spend_analysis_python(pos: list[dict[str, Any]], group_by: str) -> list[dict[str, Any]]:
    """Python 聚合实现（hybrid 模式）。"""
    groups: dict[str, dict[str, Any]] = {}
    for po in pos:
        if group_by == "supplier":
            key = po.get("vendor_name", "")
        else:
            key = po.get("material_category", "")
        agg = groups.setdefault(key, {
            "po_numbers": set(),
            "total_amount": 0.0,
            "unit_prices": [],
        })
        agg["po_numbers"].add(po.get("po_number", ""))
        agg["total_amount"] += float(po.get("po_amount") or 0)
        up = po.get("unit_price")
        if up is not None:
            agg["unit_prices"].append(float(up))

    result = []
    for key, agg in sorted(groups.items(), key=lambda x: x[1]["total_amount"], reverse=True):
        prices = agg["unit_prices"]
        result.append({
            "group_by": group_by,
            "group_key": key,
            "order_count": len(agg["po_numbers"]),
            "total_amount": round(agg["total_amount"], 2),
            "avg_unit_price": round(sum(prices) / len(prices), 2) if prices else 0.0,
        })
    return result


@tool
async def calculate_spend_analysis(
    group_by: str = "category",
    days: int = 30,
) -> str:
    """按维度聚合采购支出分析（按品类或按供应商汇总）。

    适用场景：需要了解采购支出的分布和集中情况。
    返回内容：分组聚合结果（订单数、总金额、平均单价），按金额降序排列。
    不适用：分析供应商依赖度请用 analyze_vendor_concentration。

    Args:
        group_by: 聚合维度，"category"（按品类）或 "supplier"（按供应商），默认 category。
        days: 分析最近 N 天内的数据，默认 30 天。

    Returns:
        JSON 格式的支出分析结果字符串。
    """
    result = await _calculate_spend_analysis_impl(group_by, days)
    return _clip_and_dump(result)
