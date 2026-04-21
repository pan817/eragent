"""Graph 实体操作工具（3个）。

通过 Cypher 查询实体属性、时间线和关系网络。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from modules.p2p.tools._inject import _get_graphiti_client
from modules.p2p.tools._output import _clip_and_dump
from modules.p2p.tools.graph._resolve import resolve_entity_id, _extract_records


# ── 1. get_entity_detail ───────────────────────────────────────────


@tool
async def get_entity_detail(
    entity_type: str,
    entity_id: str,
) -> str:
    """查询指定实体的完整属性和直接关联关系摘要。

    适用场景：已知实体类型和 ID，需要了解该实体的详细信息及其一跳邻居概况。
    返回内容：实体全部属性 + 关联列表（关系类型、方向、邻居类型和 ID）。
    不适用：多跳遍历请用 query_entity_relationships；模糊搜索请用 search_knowledge_graph。

    Args:
        entity_type: 实体类型（如 Supplier / PurchaseOrder / Invoice / Payment / Receipt / Material / Contract）。
        entity_id: 实体唯一标识（如供应商 ID "V001"、PO 号 "PO-001"）。

    Returns:
        JSON 格式的实体详情 + 关联摘要。
    """
    client = _get_graphiti_client()
    resolved_id = await resolve_entity_id(client, entity_type, entity_id)
    cypher = """
    MATCH (n:Entity {entity_type: $type, entity_id: $id})
    OPTIONAL MATCH (n)-[r]-(neighbor:Entity)
    RETURN properties(n) AS entity,
           collect(DISTINCT {
               relationship: COALESCE(r.name, type(r)),
               direction: CASE WHEN startNode(r) = n THEN 'outgoing' ELSE 'incoming' END,
               neighbor_type: neighbor.entity_type,
               neighbor_id: neighbor.entity_id
           }) AS connections
    """
    result = await client.execute_cypher(cypher, {"type": entity_type, "id": resolved_id})
    records = _extract_records(result)
    if not records:
        return _clip_and_dump({"entity": None, "connections": [], "message": f"{entity_type} {entity_id} not found (tried ID: {resolved_id})"})
    record = records[0]
    return _clip_and_dump({
        "entity": record.get("entity", {}),
        "connections": record.get("connections", []),
    })


# ── 2. query_entity_timeline ──────────────────────────────────────


@tool
async def query_entity_timeline(
    entity_type: str,
    entity_id: str,
    direction: str = "both",
) -> str:
    """查询实体的完整时间线，展示其生命周期中的所有关联事件。

    适用场景：需要了解某个实体（如 PO、供应商）从创建到完成的全部事件序列。
    返回内容：按时间排序的事件列表（含关联实体类型、ID、时间戳、关系类型）。
    不适用：查看两个实体之间的路径请用 find_path_between。

    Args:
        entity_type: 实体类型（如 PurchaseOrder / Supplier / Invoice）。
        entity_id: 实体唯一标识。
        direction: 时间方向 - "forward"（后续事件）/ "backward"（前序事件）/ "both"（全部），默认 both。

    Returns:
        JSON 格式的时间线事件列表。
    """
    client = _get_graphiti_client()
    resolved_id = await resolve_entity_id(client, entity_type, entity_id)

    if direction == "forward":
        pattern = "(start)-[r*1..4]->(related)"
    elif direction == "backward":
        pattern = "(start)<-[r*1..4]-(related)"
    else:
        pattern = "(start)-[r*1..4]-(related)"

    cypher = f"""
    MATCH (start:Entity {{entity_type: $type, entity_id: $id}})
    MATCH path = {pattern}
    WHERE related:Entity
    WITH related, relationships(path) AS rels
    UNWIND rels AS rel
    RETURN DISTINCT related.entity_type AS event_type,
           related.entity_id AS event_id,
           related.valid_from AS event_time,
           COALESCE(rel.name, type(rel)) AS relationship
    ORDER BY related.valid_from
    """
    result = await client.execute_cypher(cypher, {"type": entity_type, "id": resolved_id})
    records = _extract_records(result)
    return _clip_and_dump(records)


# ── 3. query_entity_relationships ─────────────────────────────────


@tool
async def query_entity_relationships(
    entity_type: str,
    entity_id: str,
    depth: int = 2,
    relationship_types: str = "",
) -> str:
    """查询实体的关联关系网络，返回指定深度内的所有关联节点和关系。

    适用场景：需要探索某实体的关系网络，发现直接和间接关联。
    返回内容：关系网络（节点列表 + 边列表），含每个节点的类型/ID 和每条边的类型。
    不适用：只需要直接邻居请用 get_entity_detail（depth=1）；查两点间路径请用 find_path_between。

    Args:
        entity_type: 实体类型。
        entity_id: 实体唯一标识。
        depth: 遍历深度（1-3），默认 2。过大会返回过多数据。
        relationship_types: 限定关系类型（逗号分隔，如 "CREATES_PO,SUBMITS_INVOICE"），为空则返回全部类型。

    Returns:
        JSON 格式的关系网络数据。
    """
    client = _get_graphiti_client()
    resolved_id = await resolve_entity_id(client, entity_type, entity_id)
    clamped_depth = max(1, min(depth, 3))

    # Graphiti stores all edges as RELATES_TO with name property for business type
    rel_name_filter = ""
    if relationship_types:
        rel_types = [t.strip() for t in relationship_types.split(",") if t.strip()]
        rel_names_str = ", ".join(f"'{t}'" for t in rel_types)
        rel_name_filter = f"WHERE r.name IN [{rel_names_str}]"

    cypher = f"""
    MATCH (start:Entity {{entity_type: $type, entity_id: $id}})
    MATCH (start)-[r:RELATES_TO*1..{clamped_depth}]-(end:Entity)
    {rel_name_filter}
    WITH collect(DISTINCT {{type: end.entity_type, id: end.entity_id}}) +
         [{{type: start.entity_type, id: start.entity_id}}] AS nodes,
         collect(DISTINCT r) AS all_rels
    UNWIND all_rels AS rel_list
    UNWIND rel_list AS rel
    WITH nodes, collect(DISTINCT {{
        source: startNode(rel).entity_id,
        target: endNode(rel).entity_id,
        type: COALESCE(rel.name, type(rel))
    }}) AS edges
    RETURN nodes, edges
    """
    result = await client.execute_cypher(cypher, {"type": entity_type, "id": resolved_id})
    records = _extract_records(result)
    if not records:
        return _clip_and_dump({"nodes": [], "edges": []})
    return _clip_and_dump(records[0])


