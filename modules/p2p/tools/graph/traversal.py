"""Graph 路径遍历工具（3个）。

通过 Cypher 进行路径查询、全链路追踪和供应商画像。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from modules.p2p.tools._inject import _get_graph_schema, _get_graphiti_client
from modules.p2p.tools._output import _clip_and_dump
from modules.p2p.tools.graph._resolve import resolve_entity_id, _extract_records


# ── 1. find_path_between ──────────────────────────────────────────


@tool
async def find_path_between(
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
) -> str:
    """查询两个实体之间的最短路径，判断它们是否有关联以及关联路径是什么。

    适用场景：需要判断两个实体之间是否存在业务关联（如供应商和某张发票之间的关系链）。
    返回内容：路径上的节点序列和边类型序列；如果无路径则返回空。
    不适用：从一个点出发探索所有关联请用 query_entity_relationships。

    Args:
        from_type: 起点实体类型（如 Supplier）。
        from_id: 起点实体 ID。
        to_type: 终点实体类型（如 Invoice）。
        to_id: 终点实体 ID。

    Returns:
        JSON 格式的路径信息（节点列表 + 边列表）。
    """
    client = _get_graphiti_client()
    resolved_from = await resolve_entity_id(client, from_type, from_id)
    resolved_to = await resolve_entity_id(client, to_type, to_id)
    cypher = """
    MATCH (a:Entity {entity_type: $from_type, entity_id: $from_id})
    MATCH (b:Entity {entity_type: $to_type, entity_id: $to_id})
    MATCH path = shortestPath((a)-[*..6]-(b))
    RETURN [n IN nodes(path) | {type: n.entity_type, id: n.entity_id}] AS nodes,
           [r IN relationships(path) | COALESCE(r.name, type(r))] AS edges
    """
    result = await client.execute_cypher(cypher, {
        "from_type": from_type, "from_id": resolved_from,
        "to_type": to_type, "to_id": resolved_to,
    })
    records = _extract_records(result)
    if not records:
        return _clip_and_dump({"nodes": [], "edges": [], "message": "no path found"})
    return _clip_and_dump(records[0])


# ── 2. trace_procurement_chain ────────────────────────────────────


@tool
async def trace_procurement_chain(
    entity_type: str,
    entity_id: str,
) -> str:
    """从任意实体出发追踪完整的采购链路（Supplier -> PO -> Receipt -> Invoice -> Payment）。

    适用场景：需要了解某个采购实体的完整业务流转路径，包括前序和后续环节。
    返回内容：按时间排序的链路节点列表，含每个节点的类型、ID、时间戳和关系类型。
    不适用：只查直接邻居请用 get_entity_detail；查两个特定实体间的路径请用 find_path_between。

    Args:
        entity_type: 起点实体类型（如 PurchaseOrder / Invoice / Payment / Supplier / Receipt）。
        entity_id: 起点实体唯一标识。

    Returns:
        JSON 格式的采购链路（按时间排序的节点序列）。
    """
    client = _get_graphiti_client()
    resolved_id = await resolve_entity_id(client, entity_type, entity_id)
    cypher = """
    MATCH (start:Entity {entity_type: $type, entity_id: $id})
    MATCH path = (start)-[*1..6]-(related:Entity)
    WITH related, relationships(path) AS rels, length(path) AS dist
    UNWIND rels AS rel
    RETURN DISTINCT related.entity_type AS type,
           related.entity_id AS id,
           related.valid_from AS time,
           COALESCE(rel.name, type(rel)) AS relationship,
           dist AS distance
    ORDER BY related.valid_from, dist
    LIMIT 100
    """
    result = await client.execute_cypher(cypher, {"type": entity_type, "id": resolved_id})
    records = _extract_records(result)
    return _clip_and_dump(records)


# ── 3. query_supplier_profile ─────────────────────────────────────


@tool
async def query_supplier_profile(
    vendor_id: str,
) -> str:
    """获取供应商的全景画像，通过图关系遍历聚合采购、交付、发票、付款、竞价等多维度信息。

    适用场景：需要全面了解一个供应商的业务关系全貌和各维度指标。
    返回内容：供应商基本信息 + 关联统计（PO 数/收货数/发票数/付款数/竞价数/站点信息）。
    不适用：仅查供应商主数据请用 query_vendor_master；对比多个供应商请用 compare_entities。

    Args:
        vendor_id: 供应商 ID（如 "V001"）。

    Returns:
        JSON 格式的供应商画像数据。
    """
    client = _get_graphiti_client()
    gs = _get_graph_schema()
    cypher = f"""
    MATCH (s:Entity {{entity_type: '{gs.node("supplier")}', entity_id: $vendor_id}})
    OPTIONAL MATCH (s)-[r1:RELATES_TO {{name: '{gs.edge("creates_po")}'}}]->(po:Entity)
    OPTIONAL MATCH (po)-[r2:RELATES_TO {{name: '{gs.edge("contains_line")}'}}]->(line:Entity)
    OPTIONAL MATCH (line)<-[r3:RELATES_TO {{name: '{gs.edge("receives_line")}'}}]-(rcv:Entity)
    OPTIONAL MATCH (s)-[r4:RELATES_TO {{name: '{gs.edge("submits_invoice")}'}}]->(inv:Entity)
    OPTIONAL MATCH (inv)<-[r5:RELATES_TO {{name: '{gs.edge("pays_invoice")}'}}]-(pmt:Entity)
    OPTIONAL MATCH (s)-[r6:RELATES_TO {{name: '{gs.edge("bids_on")}'}}]->(auction:Entity)
    OPTIONAL MATCH (s)-[r7:RELATES_TO {{name: '{gs.edge("has_site")}'}}]->(site:Entity)
    RETURN properties(s) AS supplier,
           count(DISTINCT po) AS po_count,
           count(DISTINCT line) AS line_count,
           count(DISTINCT rcv) AS receipt_count,
           count(DISTINCT inv) AS invoice_count,
           count(DISTINCT pmt) AS payment_count,
           count(DISTINCT auction) AS auction_count,
           collect(DISTINCT {{site_id: site.entity_id, site_code: site.vendor_site_code, city: site.city}}) AS sites
    """
    result = await client.execute_cypher(cypher, {"vendor_id": vendor_id})
    records = _extract_records(result)
    if not records:
        return _clip_and_dump({"vendor_id": vendor_id, "message": "supplier not found"})
    return _clip_and_dump(records[0])
