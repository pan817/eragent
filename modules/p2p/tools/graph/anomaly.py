"""Graph 异常检测工具（2个）。

通过 Cypher 模式匹配检测结构性异常，以及评估风险传播影响面。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from modules.p2p.tools._inject import _get_graph_schema, _get_graphiti_client
from modules.p2p.tools._output import _clip_and_dump


def _extract_records(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return result
    if hasattr(result, "records"):
        return [dict(r) for r in result.records]
    return []


# ── Anomaly Rules Registry ────────────────────────────────────────

def _build_anomaly_rules() -> dict[str, str]:
    gs = _get_graph_schema()
    n = gs.node
    e = gs.edge
    return {
        "missing_receipt": f"""
            MATCH (po:Entity {{entity_type:'{n("purchase_order")}'}})
              -[:RELATES_TO {{name:'{e("contains_line")}'}}]->(line:Entity)
            WHERE NOT (line)<-[:RELATES_TO {{name:'{e("receives_line")}'}}]-(:Entity)
              AND po.valid_from >= datetime() - duration({{days: $days}})
            RETURN po.po_number AS entity, po.entity_id AS po_id,
                   'missing_receipt' AS anomaly_type, 'PO line has no receipt' AS description
        """,
        "missing_invoice": f"""
            MATCH (line:Entity)<-[:RELATES_TO {{name:'{e("receives_line")}'}}]-(rcv:Entity)
            WHERE NOT (line)<-[:RELATES_TO {{name:'{e("invoices_line")}'}}]-(:Entity)
              AND rcv.valid_from >= datetime() - duration({{days: $days}})
            RETURN line.entity_id AS entity, rcv.po_number AS po_number,
                   'missing_invoice' AS anomaly_type, 'Received but no invoice' AS description
        """,
        "orphan_payment": f"""
            MATCH (pmt:Entity {{entity_type:'{n("payment")}'}})
            WHERE NOT (pmt)-[:RELATES_TO {{name:'{e("pays_invoice")}'}}]->(:Entity)
              AND pmt.valid_from >= datetime() - duration({{days: $days}})
            RETURN pmt.check_number AS entity, pmt.amount AS amount,
                   'orphan_payment' AS anomaly_type, 'Payment not linked to any invoice' AS description
        """,
        "orphan_node": """
            MATCH (n:Entity)
            WHERE NOT (n)--()
            RETURN n.entity_type AS entity_type, n.entity_id AS entity,
                   'orphan_node' AS anomaly_type, 'Entity has no relationships' AS description
        """,
        "process_skip": f"""
            MATCH (po:Entity {{entity_type:'{n("purchase_order")}'}})
              -[:RELATES_TO {{name:'{e("contains_line")}'}}]->(line:Entity)
            WHERE (line)<-[:RELATES_TO {{name:'{e("invoices_line")}'}}]-()
              -[:RELATES_TO {{name:'{e("belongs_to_invoice")}'}}]->()
              <-[:RELATES_TO {{name:'{e("pays_invoice")}'}}]-()
              AND NOT (line)<-[:RELATES_TO {{name:'{e("receives_line")}'}}]-()
              AND po.valid_from >= datetime() - duration({{days: $days}})
            RETURN po.po_number AS entity, po.entity_id AS po_id,
                   'process_skip' AS anomaly_type, 'Invoiced/paid but no receipt (skipped receiving)' AS description
        """,
        "invoice_without_po": f"""
            MATCH (s:Entity {{entity_type:'{n("supplier")}'}})
              -[:RELATES_TO {{name:'{e("submits_invoice")}'}}]->(inv:Entity)
            WHERE NOT (:Entity)-[:RELATES_TO {{name:'{e("belongs_to_invoice")}'}}]->(inv)
              AND inv.valid_from >= datetime() - duration({{days: $days}})
            RETURN inv.invoice_num AS entity, s.entity_id AS vendor_id,
                   'invoice_without_po' AS anomaly_type, 'Invoice has no PO line linkage' AS description
        """,
        "unauthorized_payment": f"""
            MATCH (pmt:Entity {{entity_type:'{n("payment")}'}})
              -[:RELATES_TO {{name:'{e("pays_invoice")}'}}]->(inv:Entity)
              <-[:RELATES_TO {{name:'{e("submits_invoice")}'}}]-(s:Entity {{entity_type:'{n("supplier")}'}})
            WHERE NOT (s)-[:RELATES_TO {{name:'{e("creates_po")}'}}]->(:Entity)
              AND pmt.valid_from >= datetime() - duration({{days: $days}})
            RETURN s.entity_id AS entity, pmt.check_number AS payment,
                   'unauthorized_payment' AS anomaly_type, 'Supplier received payment but has no PO' AS description
        """,
        "split_order": f"""
            MATCH (s:Entity {{entity_type:'{n("supplier")}'}})
              -[:RELATES_TO {{name:'{e("creates_po")}'}}]->(po:Entity)
            WHERE po.valid_from >= datetime() - duration({{days: $days}})
              AND po.total_amount < 10000
            WITH s, collect(po) AS pos, count(po) AS po_count
            WHERE po_count >= 5
            RETURN s.entity_id AS entity, s.vendor_name AS vendor_name, po_count,
                   [p IN pos | p.po_number][..10] AS sample_po_numbers,
                   'split_order' AS anomaly_type, 'Multiple small POs from same supplier (possible split)' AS description
        """,
        "related_party": f"""
            MATCH (s1:Entity {{entity_type:'{n("supplier")}'}})
              -[:RELATES_TO {{name:'{e("has_site")}'}}]->(site:Entity)
              <-[:RELATES_TO {{name:'{e("has_site")}'}}]-(s2:Entity {{entity_type:'{n("supplier")}'}})
            WHERE s1.entity_id < s2.entity_id
            MATCH (s1)-[:RELATES_TO {{name:'{e("bids_on")}'}}]->(a:Entity {{entity_type:'{n("auction")}'}})
              <-[:RELATES_TO {{name:'{e("bids_on")}'}}]-(s2)
            RETURN s1.vendor_name AS supplier_1, s2.vendor_name AS supplier_2,
                   site.vendor_site_code AS shared_site, a.auction_title AS auction,
                   'related_party' AS anomaly_type, 'Suppliers share site and bid on same auction' AS description
        """,
        "suspicious_supplier": f"""
            MATCH (s:Entity {{entity_type:'{n("supplier")}'}})
              -[:RELATES_TO {{name:'{e("submits_invoice")}'}}]->(inv:Entity)
              <-[:RELATES_TO {{name:'{e("pays_invoice")}'}}]-(:Entity)
            WHERE NOT (s)-[:RELATES_TO {{name:'{e("has_site")}'}}]->(:Entity)
              AND NOT (s)-[:RELATES_TO {{name:'{e("bids_on")}'}}]->(:Entity)
            WITH s, count(DISTINCT inv) AS invoice_count
            RETURN s.entity_id AS entity, s.vendor_name AS vendor_name, invoice_count,
                   'suspicious_supplier' AS anomaly_type, 'Paid supplier with no site, no auction history' AS description
        """,
    }


# ── 1. detect_graph_anomalies ─────────────────────────────────────


@tool
async def detect_graph_anomalies(
    scope: str = "all",
    time_range_days: int = 30,
) -> str:
    """基于图结构检测采购流程中的结构性异常模式（非数值偏差）。

    适用场景：需要发现数据缺失、流程违规、可疑交易模式等结构性问题。
    返回内容：异常实体列表（含异常类型、实体 ID、描述）。
    不适用：数值偏差检测（价格差异）用 run_price_variance_analysis；
            数量偏差检测（超量收货）用 analyze_receipt_anomalies。

    Args:
        scope: 检测范围，可选值：
            "all" — 执行全部检测
            "missing_receipt" — PO 行无收货记录
            "missing_invoice" — 有收货但无发票
            "orphan_payment" — 付款无关联发票
            "orphan_node" — 无任何关系的孤立数据
            "process_skip" — 跳过收货直接开票付款
            "invoice_without_po" — 发票无关联采购订单
            "unauthorized_payment" — 未授权供应商收款
            "split_order" — 疑似分拆订单规避审批
            "related_party" — 共享站点供应商互相竞价
            "suspicious_supplier" — 有付款但无站点/竞价的供应商
        time_range_days: 检查时间范围（天），默认 30。

    Returns:
        JSON 格式的异常检测结果。
    """
    client = _get_graphiti_client()
    rules = _build_anomaly_rules()

    if scope == "all":
        rules_to_run = list(rules.keys())
    elif scope in rules:
        rules_to_run = [scope]
    else:
        return _clip_and_dump({"error": f"Unknown scope: {scope}", "valid_scopes": list(rules.keys())})

    all_anomalies: list[dict[str, Any]] = []
    for rule_name in rules_to_run:
        cypher = rules[rule_name]
        try:
            result = await client.execute_cypher(cypher, {"days": time_range_days})
            records = _extract_records(result)
            all_anomalies.extend(records)
        except Exception as e:
            all_anomalies.append({
                "anomaly_type": rule_name,
                "error": str(e),
            })

    return _clip_and_dump(all_anomalies)


# ── 2. query_risk_impact ──────────────────────────────────────────

def _build_risk_queries() -> dict[str, str]:
    gs = _get_graph_schema()
    n = gs.node
    e = gs.edge
    return {
        "supplier_risk": f"""
            MATCH (s:Entity {{entity_type:'{n("supplier")}', entity_id: $entity_id}})
              -[:RELATES_TO*1..4]->(affected:Entity)
            RETURN affected.entity_type AS type, affected.entity_id AS id,
                   count(*) AS path_count
            ORDER BY path_count DESC
            LIMIT 50
        """,
        "material_disruption": f"""
            MATCH (m:Entity {{entity_type:'{n("material")}', entity_id: $entity_id}})
              <-[:RELATES_TO {{name:'{e("orders_material")}'}}]-(line:Entity)
              <-[:RELATES_TO {{name:'{e("contains_line")}'}}]-(po:Entity)
            OPTIONAL MATCH (m)<-[:RELATES_TO {{name:'{e("contract_covers")}'}}]-(cl:Entity)
              <-[:RELATES_TO {{name:'{e("contains_contract_line")}'}}]-(c:Entity)
            RETURN po.po_number AS po_number, line.item_description AS item,
                   line.quantity AS quantity,
                   c.contract_number AS backup_contract,
                   CASE WHEN c IS NULL THEN 'no_alternative' ELSE 'has_contract' END AS risk_level
        """,
        "payment_chain": f"""
            MATCH (inv:Entity {{entity_type:'{n("invoice")}', invoice_num: $entity_id}})
              <-[:RELATES_TO {{name:'{e("submits_invoice")}'}}]-(s:Entity)
            MATCH (s)-[:RELATES_TO {{name:'{e("submits_invoice")}'}}]->(other_inv:Entity)
            WHERE other_inv.entity_id <> inv.entity_id
            OPTIONAL MATCH (other_inv)<-[:RELATES_TO {{name:'SCHEDULED_FOR'}}]-(ps:Entity {{payment_status_flag: 'N'}})
            RETURN other_inv.invoice_num AS invoice_num, other_inv.invoice_amount AS amount,
                   ps.due_date AS due_date, ps.amount_remaining AS remaining,
                   'chain_risk' AS risk_type
        """,
    }


@tool
async def query_risk_impact(
    impact_type: str,
    entity_id: str,
) -> str:
    """评估风险传播影响面：指定实体出现问题时，受影响的关联实体有哪些。

    适用场景：供应商被标记高风险、物料断供、发票被拒付时，需要评估波及范围。
    返回内容：受影响的实体列表（含类型、ID、影响路径数或风险等级）。
    不适用：检测结构性异常请用 detect_graph_anomalies。

    Args:
        impact_type: 风险类型，可选值：
            "supplier_risk" — 供应商风险影响面（entity_id 为供应商 ID）
            "material_disruption" — 物料供应中断评估（entity_id 为物料 ID）
            "payment_chain" — 付款连锁风险（entity_id 为发票号）
        entity_id: 对应的实体 ID（供应商 ID / 物料 ID / 发票号）。

    Returns:
        JSON 格式的风险影响评估结果。
    """
    risk_queries = _build_risk_queries()
    if impact_type not in risk_queries:
        return _clip_and_dump({"error": f"Unknown impact_type: {impact_type}", "valid_types": list(risk_queries.keys())})

    client = _get_graphiti_client()
    cypher = risk_queries[impact_type]
    result = await client.execute_cypher(cypher, {"entity_id": entity_id})
    records = _extract_records(result)
    return _clip_and_dump({"impact_type": impact_type, "entity_id": entity_id, "affected": records})
