"""Graph 对比分析工具（3个）。

通过 Cypher 进行多实体对比、合同覆盖查询和竞争供应商分析。
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


# ── 1. compare_entities ───────────────────────────────────────────


@tool
async def compare_entities(
    entity_type: str,
    entity_ids: str,
    dimensions: str = "",
) -> str:
    """对比多个同类型实体在各维度的表现，通过图关系并行遍历生成结构化对比数据。

    适用场景：需要对比多个供应商/采购订单的表现差异（如交付、定价、合规）。
    返回内容：每个实体的关系统计指标并排展示（PO 数、收货数、发票数、竞价数等）。
    不适用：只查单个供应商画像请用 query_supplier_profile。

    Args:
        entity_type: 实体类型（当前支持 Supplier）。
        entity_ids: 逗号分隔的实体 ID 列表（如 "V001,V002,V003"）。
        dimensions: 对比维度（预留参数，当前忽略），为空则全维度对比。

    Returns:
        JSON 格式的对比结果。
    """
    client = _get_graphiti_client()
    ids = [eid.strip() for eid in entity_ids.split(",") if eid.strip()]

    gs = _get_graph_schema()
    if entity_type == gs.node("supplier"):
        cypher = f"""
        MATCH (s:Entity {{entity_type:'{gs.node("supplier")}'}})
        WHERE s.entity_id IN $ids
        OPTIONAL MATCH (s)-[:RELATES_TO {{name:'{gs.edge("creates_po")}'}}]->(po:Entity)
        OPTIONAL MATCH (po)-[:RELATES_TO {{name:'{gs.edge("contains_line")}'}}]->(line:Entity)
          <-[:RELATES_TO {{name:'{gs.edge("receives_line")}'}}]-(rcv:Entity)
        OPTIONAL MATCH (s)-[:RELATES_TO {{name:'{gs.edge("submits_invoice")}'}}]->(inv:Entity)
        OPTIONAL MATCH (s)-[:RELATES_TO {{name:'{gs.edge("bids_on")}'}}]->(auction:Entity)
        RETURN s.entity_id AS vendor_id, s.vendor_name AS vendor_name,
               count(DISTINCT po) AS po_count,
               count(DISTINCT rcv) AS receipt_count,
               count(DISTINCT inv) AS invoice_count,
               count(DISTINCT auction) AS auction_count
        ORDER BY po_count DESC
        """
        result = await client.execute_cypher(cypher, {"ids": ids})
    else:
        # Generic: count relationships per entity
        cypher = """
        MATCH (n:Entity {entity_type: $type})
        WHERE n.entity_id IN $ids
        OPTIONAL MATCH (n)-[r]-()
        RETURN n.entity_id AS entity_id, n.entity_type AS entity_type,
               count(r) AS total_connections,
               collect(DISTINCT type(r)) AS relationship_types
        """
        result = await client.execute_cypher(cypher, {"type": entity_type, "ids": ids})

    records = _extract_records(result)
    return _clip_and_dump(records)


# ── 2. find_contract_coverage ─────────────────────────────────────


@tool
async def find_contract_coverage(
    vendor_id: str = "",
    material_id: str = "",
) -> str:
    """查询采购行是否有合同价格覆盖，识别无合同保障的采购风险。

    适用场景：需要了解哪些采购行有合同定价、哪些没有合同覆盖（存在价格风险）。
    返回内容：采购行列表（含 PO 号、物料、合同号、合同价格、覆盖状态 covered/uncovered）。
    不适用：分析价格偏差请用 run_price_variance_analysis。

    Args:
        vendor_id: 供应商 ID，为空则查全部供应商的采购行。
        material_id: 物料 ID，为空则查全部物料。

    Returns:
        JSON 格式的合同覆盖分析结果。
    """
    client = _get_graphiti_client()

    wheres = []
    params: dict[str, Any] = {}
    if vendor_id:
        wheres.append("po.vendor_id = $vendor_id")
        params["vendor_id"] = vendor_id
    if material_id:
        wheres.append("m.entity_id = $material_id")
        params["material_id"] = material_id

    where_clause = "WHERE " + " AND ".join(wheres) if wheres else ""

    gs = _get_graph_schema()
    cypher = f"""
    MATCH (line:Entity {{entity_type:'{gs.node("po_line")}'}})
      <-[:RELATES_TO {{name:'{gs.edge("contains_line")}'}}]-(po:Entity)
    OPTIONAL MATCH (line)-[:RELATES_TO {{name:'{gs.edge("orders_material")}'}}]->(m:Entity)
      <-[:RELATES_TO {{name:'{gs.edge("contract_covers")}'}}]-(cl:Entity)
      <-[:RELATES_TO {{name:'{gs.edge("contains_contract_line")}'}}]-(c:Entity)
    {where_clause}
    RETURN po.po_number AS po_number, po.entity_id AS vendor_id,
           line.item_description AS item, line.unit_price AS unit_price,
           m.segment1 AS material_code,
           c.contract_number AS contract_number,
           cl.price_negotiated AS contract_price,
           CASE WHEN c IS NULL THEN 'uncovered' ELSE 'covered' END AS coverage
    ORDER BY coverage, po.po_number
    LIMIT 100
    """
    result = await client.execute_cypher(cypher, params)
    records = _extract_records(result)
    return _clip_and_dump(records)


# ── 3. find_competing_suppliers ───────────────────────────────────


@tool
async def find_competing_suppliers(
    vendor_id: str = "",
    auction_id: str = "",
) -> str:
    """查询同一竞价中的竞争供应商，分析供应商之间的竞争关系。

    适用场景：需要了解某供应商在竞价中面对的竞争对手，或某次竞价的全部参与者。
    返回内容：竞价信息 + 参与供应商列表（含报价和中标状态）。
    不适用：查看供应商全景画像请用 query_supplier_profile。

    Args:
        vendor_id: 供应商 ID，查该供应商参与的所有竞价及其竞争者。
        auction_id: 竞价 ID，查该竞价的全部参与者。至少提供一个参数。

    Returns:
        JSON 格式的竞争分析结果。
    """
    client = _get_graphiti_client()

    gs = _get_graph_schema()
    if vendor_id:
        cypher = f"""
        MATCH (s:Entity {{entity_type:'{gs.node("supplier")}', entity_id: $vendor_id}})
          -[:RELATES_TO {{name:'{gs.edge("bids_on")}'}}]->(a:Entity {{entity_type:'{gs.node("auction")}'}})
        MATCH (competitor:Entity {{entity_type:'{gs.node("supplier")}'}})
          -[:RELATES_TO {{name:'{gs.edge("bids_on")}'}}]->(a)
        WHERE competitor.entity_id <> $vendor_id
        OPTIONAL MATCH (a)<-[:RELATES_TO {{name:'{gs.edge("has_bid")}'}}]-(bid:Entity)
        RETURN a.document_number AS auction_id, a.auction_title AS auction_title,
               a.auction_status AS status,
               competitor.entity_id AS competitor_id,
               competitor.vendor_name AS competitor_name,
               bid.bid_total AS bid_amount, bid.award_status AS award_status
        ORDER BY a.document_number
        """
        params: dict[str, Any] = {"vendor_id": vendor_id}
    elif auction_id:
        cypher = f"""
        MATCH (s:Entity {{entity_type:'{gs.node("supplier")}'}})
          -[:RELATES_TO {{name:'{gs.edge("bids_on")}'}}]->(a:Entity {{entity_type:'{gs.node("auction")}', entity_id: $auction_id}})
        OPTIONAL MATCH (a)<-[:RELATES_TO {{name:'{gs.edge("has_bid")}'}}]-(bid:Entity)
        RETURN a.auction_title AS auction_title, a.auction_status AS status,
               s.entity_id AS vendor_id, s.vendor_name AS vendor_name,
               bid.bid_total AS bid_amount, bid.award_status AS award_status
        ORDER BY bid.bid_total
        """
        params = {"auction_id": auction_id}
    else:
        return _clip_and_dump({"error": "Please provide vendor_id or auction_id"})

    result = await client.execute_cypher(cypher, params)
    records = _extract_records(result)
    return _clip_and_dump(records)
