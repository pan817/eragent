"""PG 聚合分析工具（5个）。

每个工具内部按模式分支：
- postgresql 模式 → SQL 聚合（性能最优）
- hybrid 模式 → QueryBackend 取数据 + Python 聚合
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any

from langchain.tools import tool

from modules.p2p.tools._inject import _get_query_backend, _get_repository
from modules.p2p.tools._output import _clip_and_dump


def _get_mode() -> str:
    from config.settings import get_settings
    return get_settings().graphiti_etl.query_backend


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _parse_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


# ── 1. 收货异常分析 ────────────────────────────────────────────────


async def _receipt_anomalies_python(
    vendor_id: str, po_number: str, days: int
) -> list[dict[str, Any]]:
    """Python 实现：QueryBackend 取数据后比对。"""
    backend = _get_query_backend()
    # 有明确实体时不加时间窗口，避免过滤掉目标数据
    effective_days = 0 if (po_number or vendor_id) else days
    receipts = await backend.query_receipts(
        vendor_id=vendor_id, po_number=po_number, days=effective_days
    )
    pos = await backend.query_purchase_orders(
        vendor_id=vendor_id, po_number=po_number, days=effective_days
    )

    po_map: dict[str, dict[str, Any]] = {}
    for po in pos:
        pn = po.get("po_number", "")
        po_map[pn] = {
            "po_quantity": _safe_float(po.get("po_quantity")),
            "required_date": _parse_date(po.get("required_date")),
        }

    anomalies: list[dict[str, Any]] = []
    for rcv in receipts:
        pn = rcv.get("po_number", "")
        po_info = po_map.get(pn)
        if po_info is None:
            continue

        rcv_qty = _safe_float(rcv.get("gr_quantity"))
        po_qty = po_info["po_quantity"]
        rej_qty = 0.0
        if not rcv.get("quality_passed", True):
            rej_qty = rcv_qty * 0.05

        issues: list[str] = []
        if po_qty > 0 and rcv_qty > po_qty:
            over_pct = ((rcv_qty - po_qty) / po_qty) * 100
            issues.append(f"超量收货 {over_pct:.1f}%")
        if rej_qty > 0:
            issues.append(f"拒收 {rej_qty:.0f} 件")

        rcv_date = _parse_date(rcv.get("receipt_date"))
        req_date = po_info["required_date"]
        if rcv_date and req_date and rcv_date > req_date:
            delay = (rcv_date - req_date).days
            issues.append(f"延迟 {delay} 天")

        if issues:
            anomalies.append({
                "receipt_id": rcv.get("gr_number", ""),
                "po_number": pn,
                "vendor_id": rcv.get("vendor_id", ""),
                "po_quantity": po_qty,
                "received_quantity": rcv_qty,
                "rejected_quantity": rej_qty,
                "receipt_date": str(rcv.get("receipt_date", "")),
                "issues": issues,
            })

    return anomalies


@tool
async def analyze_receipt_anomalies(
    vendor_id: str = "",
    po_number: str = "",
    days: int = 30,
) -> str:
    """分析收货异常：超量收货、拒收、延迟收货。

    适用场景：需要检查收货数量是否超出 PO 数量、是否有拒收、是否延迟交付。
    返回内容：异常收货列表（含异常类型、PO 数量、实收数量、延迟天数）。
    不适用：检测"有 PO 无收货"的结构缺失请用 detect_graph_anomalies(scope="missing_receipt")。

    Args:
        vendor_id: 供应商 ID，空则分析全部。
        po_number: 采购订单号，空则分析全部。
        days: 分析最近 N 天，默认 30。

    Returns:
        JSON 格式的收货异常列表。
    """
    if _get_mode() == "postgresql":
        repo = _get_repository()
        result = await asyncio.to_thread(
            repo.analyze_receipt_anomalies, vendor_id, po_number, days
        )
    else:
        result = await _receipt_anomalies_python(vendor_id, po_number, days)
    return _clip_and_dump(result)


# ── 2. 重复发票检测 ────────────────────────────────────────────────


async def _duplicate_invoices_python(
    vendor_id: str, days: int
) -> list[dict[str, Any]]:
    """Python 实现：QueryBackend 取数据后分组检测。"""
    backend = _get_query_backend()
    # 有明确供应商时不加时间窗口，避免跨月重复发票漏检
    effective_days = 0 if vendor_id else days
    invoices = await backend.query_invoices(vendor_id=vendor_id, days=effective_days)

    groups: dict[tuple[str, float], list[dict[str, Any]]] = {}
    for inv in invoices:
        key = (inv.get("vendor_id", ""), _safe_float(inv.get("invoice_amount")))
        groups.setdefault(key, []).append(inv)

    duplicates: list[dict[str, Any]] = []
    for (sid, amount), group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: str(x.get("creation_date", "")))
        for i in range(len(group) - 1):
            for j in range(i + 1, len(group)):
                d_a = _parse_date(group[i].get("creation_date"))
                d_b = _parse_date(group[j].get("creation_date"))
                gap = abs((d_b - d_a).days) if d_a and d_b else 999
                if gap <= 3:
                    duplicates.append({
                        "vendor_id": sid,
                        "vendor_name": group[i].get("vendor_name", ""),
                        "amount": amount,
                        "invoice_a": group[i].get("invoice_num", ""),
                        "invoice_b": group[j].get("invoice_num", ""),
                        "date_a": str(d_a or ""),
                        "date_b": str(d_b or ""),
                        "date_gap_days": gap,
                    })
    return duplicates


@tool
async def detect_duplicate_invoices(
    vendor_id: str = "",
    days: int = 30,
) -> str:
    """检测重复发票：同供应商、同金额、日期相近（±3天）的发票对。

    适用场景：需要发现可能的重复开票或重复录入。
    返回内容：疑似重复发票对列表（含两张发票号、金额、日期差）。
    不适用：检测无 PO 关联的发票请用 detect_graph_anomalies(scope="invoice_without_po")。

    Args:
        vendor_id: 供应商 ID，空则检查全部。
        days: 分析最近 N 天，默认 30。

    Returns:
        JSON 格式的疑似重复发票列表。
    """
    if _get_mode() == "postgresql":
        repo = _get_repository()
        result = await asyncio.to_thread(
            repo.detect_duplicate_invoices, vendor_id, days
        )
    else:
        result = await _duplicate_invoices_python(vendor_id, days)
    return _clip_and_dump(result)


# ── 3. 折扣利用率分析 ──────────────────────────────────────────────


async def _discount_utilization_python(
    vendor_id: str, days: int
) -> dict[str, Any]:
    """Python 实现：QueryBackend 取数据后匹配。"""
    backend = _get_query_backend()
    # 发票和付款使用一致的时间窗口，有明确供应商时不限时间
    effective_days = 0 if vendor_id else days
    invoices = await backend.query_invoices(vendor_id=vendor_id, days=effective_days)
    payments = await backend.query_payments(vendor_id=vendor_id, days=effective_days)

    pmt_map = {p.get("invoice_num", ""): p for p in payments if p.get("invoice_num")}

    discount_rate = 0.02
    eligible = [inv for inv in invoices if inv.get("discount_due_date")]
    utilized = 0
    missed = 0
    missed_amount = 0.0
    details: list[dict[str, Any]] = []

    for inv in eligible:
        pmt = pmt_map.get(inv.get("invoice_num", ""))
        if pmt is None:
            continue
        inv_amount = _safe_float(inv.get("invoice_amount"))
        potential = inv_amount * discount_rate
        disc_due = _parse_date(inv.get("discount_due_date"))
        chk_date = _parse_date(pmt.get("check_date"))
        if disc_due and chk_date and chk_date <= disc_due:
            utilized += 1
        else:
            missed += 1
            missed_amount += potential
            days_late = (chk_date - disc_due).days if chk_date and disc_due else 0
            details.append({
                "invoice_num": inv.get("invoice_num", ""),
                "vendor_name": inv.get("vendor_name", ""),
                "invoice_amount": inv_amount,
                "discount_due_date": str(disc_due or ""),
                "check_date": str(chk_date or ""),
                "days_late": days_late,
                "missed_saving": round(potential, 2),
            })

    total = len(eligible)
    return {
        "total_eligible": total,
        "utilized": utilized,
        "missed": missed,
        "utilization_rate": round(utilized / total * 100, 1) if total > 0 else 0,
        "total_missed_saving": round(missed_amount, 2),
        "missed_details": details[:20],
    }


@tool
async def analyze_discount_utilization(
    vendor_id: str = "",
    days: int = 30,
) -> str:
    """分析早付折扣利用率：哪些发票有折扣机会但未及时付款。

    适用场景：需要评估是否充分利用了供应商提供的早付折扣，减少不必要的成本损失。
    返回内容：折扣利用率统计（总数、已利用、错过）+ 错过折扣的发票明细（含损失金额）。
    不适用：检查付款是否超期请用 run_payment_compliance_check。

    Args:
        vendor_id: 供应商 ID，空则分析全部。
        days: 分析最近 N 天，默认 30。

    Returns:
        JSON 格式的折扣利用分析结果。
    """
    if _get_mode() == "postgresql":
        repo = _get_repository()
        result = await asyncio.to_thread(
            repo.analyze_discount_utilization, vendor_id, days
        )
    else:
        result = await _discount_utilization_python(vendor_id, days)
    return _clip_and_dump(result)


# ── 4. 供应商集中度分析 ────────────────────────────────────────────


async def _vendor_concentration_python(
    days: int, top_n: int
) -> dict[str, Any]:
    """Python 实现：QueryBackend 取数据后聚合 + 图关系密度增强。"""
    backend = _get_query_backend()
    pos = await backend.query_purchase_orders(days=days)

    vendor_agg: dict[str, dict[str, Any]] = {}
    cat_vendors: dict[str, set[str]] = {}
    for po in pos:
        vid = po.get("vendor_id", "")
        agg = vendor_agg.setdefault(vid, {
            "vendor_id": vid,
            "vendor_name": po.get("vendor_name", ""),
            "total_spend": 0.0,
            "po_numbers": set(),
        })
        agg["total_spend"] += _safe_float(po.get("po_amount"))
        agg["po_numbers"].add(po.get("po_number", ""))
        cat = po.get("material_category", "")
        if cat:
            cat_vendors.setdefault(cat, set()).add(vid)

    grand_total = sum(a["total_spend"] for a in vendor_agg.values())
    sorted_vendors = sorted(vendor_agg.values(), key=lambda x: x["total_spend"], reverse=True)

    vendors = []
    high_dependency: list[dict[str, Any]] = []
    for v in sorted_vendors[:top_n]:
        pct = (v["total_spend"] / grand_total * 100) if grand_total > 0 else 0
        entry = {
            "vendor_id": v["vendor_id"],
            "vendor_name": v["vendor_name"],
            "total_spend": round(v["total_spend"], 2),
            "order_count": len(v["po_numbers"]),
            "spend_pct": round(pct, 1),
        }
        vendors.append(entry)
        if pct > 20:
            high_dependency.append(entry)

    single_source = [cat for cat, vids in cat_vendors.items() if len(vids) == 1]

    # 图增强：附加关系密度数据
    relationship_density = await _get_vendor_relationship_density(
        [v["vendor_id"] for v in vendors]
    )

    return {
        "grand_total_spend": round(grand_total, 2),
        "top_vendors": vendors,
        "high_dependency_vendors": high_dependency,
        "single_source_categories": single_source,
        "relationship_density": relationship_density,
    }


async def _get_vendor_relationship_density(
    vendor_ids: list[str],
) -> list[dict[str, Any]]:
    """查询供应商的关系密度（hybrid 模式增强）。"""
    from modules.p2p.tools._inject import _get_graph_schema, _get_graphiti_client

    try:
        client = _get_graphiti_client()
    except RuntimeError:
        return []

    gs = _get_graph_schema()
    cypher = f"""
    MATCH (s:Entity {{entity_type:'{gs.node("supplier")}'}})
      -[r]->(target:Entity)
    WHERE s.vendor_id IN $ids
    RETURN s.vendor_id AS vendor_id,
           count(r) AS total_connections,
           count(CASE WHEN r.name = '{gs.edge("creates_po")}' THEN 1 END) AS po_connections,
           count(CASE WHEN r.name = '{gs.edge("submits_invoice")}' THEN 1 END) AS invoice_connections,
           count(CASE WHEN r.name = '{gs.edge("bids_on")}' THEN 1 END) AS auction_connections
    ORDER BY total_connections DESC
    """
    try:
        result = await client.execute_cypher(cypher, {"ids": vendor_ids})
        if isinstance(result, list):
            return result
        if hasattr(result, "records"):
            return [dict(r) for r in result.records]
    except Exception:
        pass
    return []


@tool
async def analyze_vendor_concentration(
    days: int = 30,
    top_n: int = 10,
) -> str:
    """分析供应商集中度：采购依赖度和单一来源风险。

    适用场景：需要识别采购过度依赖少数供应商的风险，以及只有单一供应商的品类。
    返回内容：Top N 供应商（含采购占比）、高依赖供应商（>20%）、单一来源品类。
    不适用：查看供应商关系密度请用 compare_entities；查看竞争供应商请用 find_competing_suppliers。

    Args:
        days: 分析最近 N 天，默认 30。
        top_n: 返回 Top N 供应商，默认 10。

    Returns:
        JSON 格式的供应商集中度分析结果。
    """
    if _get_mode() == "postgresql":
        repo = _get_repository()
        result = await asyncio.to_thread(
            repo.analyze_vendor_concentration, days, top_n
        )
    else:
        result = await _vendor_concentration_python(days, top_n)
    return _clip_and_dump(result)


# ── 5. PO 全流程周期 ───────────────────────────────────────────────


async def _po_cycle_time_python(
    days: int, vendor_id: str
) -> dict[str, Any]:
    """Python 实现：QueryBackend 取数据后计算各阶段耗时。"""
    backend = _get_query_backend()
    pos = await backend.query_purchase_orders(vendor_id=vendor_id, days=days)
    receipts = await backend.query_receipts(vendor_id=vendor_id, days=0)
    payments = await backend.query_payments(vendor_id=vendor_id, days=0)
    invoices = await backend.query_invoices(vendor_id=vendor_id, days=0)

    rcv_by_po: dict[str, list[date]] = {}
    for r in receipts:
        pn = r.get("po_number", "")
        d = _parse_date(r.get("receipt_date"))
        if pn and d:
            rcv_by_po.setdefault(pn, []).append(d)

    inv_po_map: dict[str, str] = {}
    for inv in invoices:
        inv_po_map[inv.get("invoice_num", "")] = inv.get("po_number", "")

    pmt_by_po: dict[str, list[date]] = {}
    for p in payments:
        inv_num = p.get("invoice_num", "")
        pn = inv_po_map.get(inv_num, "")
        d = _parse_date(p.get("check_date"))
        if pn and d:
            pmt_by_po.setdefault(pn, []).append(d)

    details: list[dict[str, Any]] = []
    total_c2r = 0
    total_r2p = 0
    total_e2e = 0
    n_rcv = 0
    n_pmt = 0

    for po in pos:
        pn = po.get("po_number", "")
        created = _parse_date(po.get("creation_date"))
        if not created:
            continue
        entry: dict[str, Any] = {
            "po_number": pn,
            "vendor_name": po.get("vendor_name", ""),
            "created_date": str(created),
        }
        rcv_dates = rcv_by_po.get(pn, [])
        if rcv_dates:
            first_rcv = min(rcv_dates)
            c2r = (first_rcv - created).days
            entry["first_receipt_date"] = str(first_rcv)
            entry["create_to_receipt_days"] = c2r
            total_c2r += c2r
            n_rcv += 1

            pmt_dates = pmt_by_po.get(pn, [])
            if pmt_dates:
                first_pmt = min(pmt_dates)
                r2p = (first_pmt - first_rcv).days
                e2e = (first_pmt - created).days
                entry["first_payment_date"] = str(first_pmt)
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


@tool
async def calculate_po_cycle_time(
    days: int = 30,
    vendor_id: str = "",
) -> str:
    """计算采购订单全流程周期：创建 -> 收货 -> 付款各阶段耗时。

    适用场景：需要了解采购流程各环节的效率，识别流程瓶颈。
    返回内容：平均周期（创建到收货、收货到付款、端到端）+ 每个 PO 的明细。
    不适用：追踪单个 PO 的完整关联链路请用 trace_procurement_chain。

    Args:
        days: 分析最近 N 天的采购订单，默认 30。
        vendor_id: 供应商 ID，空则分析全部。

    Returns:
        JSON 格式的周期分析结果。
    """
    if _get_mode() == "postgresql":
        repo = _get_repository()
        result = await asyncio.to_thread(
            repo.calculate_po_cycle_time, days, vendor_id
        )
    else:
        result = await _po_cycle_time_python(days, vendor_id)
    return _clip_and_dump(result)
