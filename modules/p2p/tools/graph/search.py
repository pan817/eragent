"""Graph 搜索工具（1个）。

同时搜索两个数据路径：
1. Graphiti episode 语义搜索（自由文本：comments/descriptions）
2. Neo4j Entity 节点属性模糊匹配（结构化数据：supplier/PO/invoice 等）

合并去重后返回，确保 LLM 总能搜到相关实体。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from modules.p2p.tools._inject import _get_graphiti_client
from modules.p2p.tools._output import _clip_and_dump

from core.logging_utils import get_logger

_logger = get_logger(__name__)


async def _search_episodes(
    client: Any, query: str, max_results: int
) -> list[dict[str, Any]]:
    """Graphiti episode 语义搜索（自由文本）。"""
    try:
        results = await client.search(query, num_results=max_results)
        return results if isinstance(results, list) else []
    except Exception:
        _logger.warning("Episode search failed", exc_info=True)
        return []


async def _search_entities(
    client: Any, query: str, entity_types: list[str], max_results: int
) -> list[dict[str, Any]]:
    """Neo4j Entity 节点属性模糊搜索（结构化数据）。

    搜索字段基于 ETL registry 实际写入的属性：
    - entity_id: 所有节点的主键（Supplier=vendor_id, PO=po_header_id 等）
    - name: Graphiti 兼容名称（格式 "{entity_type} {entity_id}"）
    - summary: 节点摘要
    - vendor_name: Supplier 节点
    - po_number: PurchaseOrder 节点
    - invoice_num: Invoice 节点
    - check_number: Payment 节点
    - item_description: POLine / InvoiceLine / Material 节点
    - vendor_site_code: SupplierSite 节点
    - contract_number: Contract 节点
    - auction_title: Auction 节点
    """
    try:
        type_filter = ""
        params: dict[str, Any] = {"query": f"(?i).*{query}.*", "limit": max_results}

        if entity_types:
            type_filter = "AND n.entity_type IN $types"
            params["types"] = entity_types

        # 搜索字段均为 ETL 实际写入的属性名（无 vendor_id，用 entity_id 代替）
        cypher = f"""
        MATCH (n:Entity)
        WHERE (
            n.entity_id =~ $query
            OR n.name =~ $query
            OR n.summary =~ $query
            OR n.vendor_name =~ $query
            OR n.po_number =~ $query
            OR n.invoice_num =~ $query
            OR n.check_number =~ $query
            OR n.item_description =~ $query
            OR n.vendor_site_code =~ $query
            OR n.contract_number =~ $query
            OR n.auction_title =~ $query
            OR n.segment1 =~ $query
            OR n.description =~ $query
        ) {type_filter}
        RETURN n.entity_type AS entity_type,
               n.entity_id AS entity_id,
               n.name AS name,
               n.vendor_name AS vendor_name,
               n.po_number AS po_number,
               n.invoice_num AS invoice_num,
               n.check_number AS check_number,
               n.valid_from AS valid_from,
               n.summary AS summary
        LIMIT $limit
        """
        result = await client.execute_cypher(cypher, params)

        if isinstance(result, list):
            records = result
        elif hasattr(result, "records"):
            records = [dict(r) for r in result.records]
        else:
            records = []

        return [
            {
                "source": "entity_node",
                "entity_type": r.get("entity_type", ""),
                "entity_id": r.get("entity_id", ""),
                "name": r.get("name", ""),
                "vendor_name": r.get("vendor_name"),
                "po_number": r.get("po_number"),
                "invoice_num": r.get("invoice_num"),
                "check_number": r.get("check_number"),
                "valid_from": str(r.get("valid_from", "")),
                "summary": r.get("summary", ""),
            }
            for r in records
        ]
    except Exception:
        _logger.warning("Entity node search failed", exc_info=True)
        return []


@tool
async def search_knowledge_graph(
    query: str,
    entity_types: str = "",
    max_results: int = 10,
) -> str:
    """在知识图谱中搜索与查询相关的实体和关系（同时搜索语义和结构化数据）。

    适用场景：不确定具体实体 ID 时的探索性查询，或需要用自然语言描述查找相关信息。
    返回内容：匹配的实体列表（含类型、ID、属性摘要），来源标记为 episode（语义匹配）或 entity_node（属性匹配）。
    不适用：已知实体 ID 精确查询请用 get_entity_detail；按条件过滤请用 query_purchase_orders 等结构化工具。

    Args:
        query: 搜索查询（支持自然语言如"延迟交付的供应商"，也支持 ID 如"SUP-001"、"PO-2024"）。
        entity_types: 偏好的实体类型（逗号分隔，如 "Supplier,PurchaseOrder"）。匹配类型的结果优先排列，但不排除其他类型。为空则搜索全部。
        max_results: 最大返回结果数，默认 10。

    Returns:
        JSON 格式的搜索结果字符串。如果找到实体但需要查看其关联关系，请继续调用 get_entity_detail 或 query_entity_relationships。
    """
    client = _get_graphiti_client()

    type_list = [t.strip() for t in entity_types.split(",") if t.strip()] if entity_types else []

    search_query = query
    if type_list:
        search_query = f"{query} [types: {','.join(type_list)}]"

    # Parallel search: episodes + entity nodes
    # Entity node search always searches ALL types (no hard filter)
    episode_results = await _search_episodes(client, search_query, max_results)
    entity_results = await _search_entities(client, query, [], max_results * 2)

    # Tag episode results with source
    for r in episode_results:
        if "source" not in r:
            r["source"] = "episode"

    # If entity_types specified, tag and sort: matching types first
    if type_list:
        for r in entity_results:
            r["type_match"] = r.get("entity_type", "") in type_list

    # Merge and deduplicate (entity_id based)
    seen_ids: set[str] = set()
    merged: list[dict[str, Any]] = []

    # Entity nodes first, matching types first if specified
    if type_list:
        entity_results.sort(key=lambda r: (not r.get("type_match", False)))

    for r in entity_results:
        eid = r.get("entity_id", "")
        if eid and eid not in seen_ids:
            seen_ids.add(eid)
            merged.append(r)

    # Then episode results
    for r in episode_results:
        eid = r.get("entity_id", "")
        if not eid or eid not in seen_ids:
            if eid:
                seen_ids.add(eid)
            merged.append(r)

    final = merged[:max_results]

    # Embed action hint: guide LLM to use traversal tools for found entities
    if final:
        entity_samples = [
            f"{r['entity_type']}:{r['entity_id']}"
            for r in final[:3] if r.get("entity_type") and r.get("entity_id")
        ]
        if entity_samples:
            final.append({
                "_next_step": (
                    "搜索找到了实体，但搜索无法发现实体间的关系（关系存储在图的边中）。"
                    "你必须调用以下工具之一来查看关联实体：\n"
                    f"  get_entity_detail(entity_type='{final[0].get('entity_type','')}', "
                    f"entity_id='{final[0].get('entity_id','')}')\n"
                    f"  query_entity_relationships(entity_type='{final[0].get('entity_type','')}', "
                    f"entity_id='{final[0].get('entity_id','')}')"
                ),
            })

    return _clip_and_dump(final)
