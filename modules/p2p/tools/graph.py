"""Graph query tools — 6 LangChain tools backed by Graphiti knowledge graph.

These tools allow the Agent to query the time-series knowledge graph built
by the ETL pipeline.  They gracefully degrade (return empty/error message)
when the Graphiti client is not connected.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.tools import tool

from modules.p2p.tools._inject import _get_graphiti_client
from modules.p2p.tools._output import _clip_and_dump


# ── 1. search_knowledge_graph ────────────────────────────────


@tool
async def search_knowledge_graph(
    query: str,
    entity_types: str = "",
    time_range_days: int = 90,
    max_results: int = 10,
) -> str:
    """在知识图谱中搜索与查询相关的实体和关系。

    支持自然语言查询，返回匹配的节点、关系及其时序信息。

    Args:
        query: 自然语言搜索查询。
        entity_types: 限定搜索的实体类型（逗号分隔，如 "Supplier,PurchaseOrder"），为空则搜索全部。
        time_range_days: 时间范围（天），默认 90 天。
        max_results: 最大返回结果数。

    Returns:
        JSON 格式的搜索结果字符串。
    """
    client = _get_graphiti_client()
    search_query = query
    if entity_types:
        search_query = f"{query} [types: {entity_types}]"

    results = await client.search(search_query, num_results=max_results)
    return _clip_and_dump(results)


# ── 2. query_entity_timeline ─────────────────────────────────


@tool
async def query_entity_timeline(
    entity_type: str,
    entity_id: str,
    include_related: bool = True,
) -> str:
    """查询某个实体的完整时间线，展示其生命周期中的所有事件。

    例如：一个采购订单从创建、收货、开票到付款的完整过程。

    Args:
        entity_type: 实体类型（如 PurchaseOrder, Supplier, Invoice）。
        entity_id: 实体唯一标识。
        include_related: 是否包含关联实体的时间线事件，默认 True。

    Returns:
        JSON 格式的时间线事件列表。
    """
    client = _get_graphiti_client()
    depth = 3 if include_related else 1
    query = f"timeline of {entity_type} {entity_id}"
    results = await client.search(query, num_results=depth * 5)
    return _clip_and_dump(results)


# ── 3. query_entity_relationships ────────────────────────────


@tool
async def query_entity_relationships(
    entity_type: str,
    entity_id: str,
    depth: int = 2,
    relationship_types: str = "",
) -> str:
    """查询实体的关联关系网络，返回指定深度内的所有关联节点和关系。

    Args:
        entity_type: 实体类型。
        entity_id: 实体唯一标识。
        depth: 遍历深度（1-3），默认 2。
        relationship_types: 限定关系类型（逗号分隔），为空则返回全部。

    Returns:
        JSON 格式的关系网络数据。
    """
    client = _get_graphiti_client()
    clamped_depth = max(1, min(depth, 3))
    results = await client.get_relationships(
        entity_type, entity_id, depth=clamped_depth
    )

    if relationship_types:
        allowed = {t.strip() for t in relationship_types.split(",")}
        results = [
            r for r in results
            if r.get("edge_type", r.get("fact_type", "")) in allowed
        ]

    return _clip_and_dump(results)


# ── 4. query_supplier_profile ────────────────────────────────


@tool
async def query_supplier_profile(
    vendor_id: str,
    time_range_days: int = 180,
) -> str:
    """获取供应商的全景画像，聚合采购、交付、发票、付款、合同等多维度信息。

    通过知识图谱关系遍历获取供应商的全方位关联数据。

    Args:
        vendor_id: 供应商 ID。
        time_range_days: 统计时间范围（天），默认 180 天。

    Returns:
        JSON 格式的供应商画像数据。
    """
    client = _get_graphiti_client()

    # Gather multiple facets via graph search
    profile_query = f"supplier {vendor_id} purchase orders invoices payments contracts performance"
    results = await client.search(profile_query, num_results=15)

    return _clip_and_dump({
        "vendor_id": vendor_id,
        "time_range_days": time_range_days,
        "graph_facts": results,
    })


# ── 5. compare_entities ──────────────────────────────────────


@tool
async def compare_entities(
    entity_type: str,
    entity_ids: str,
    dimensions: str = "",
) -> str:
    """对比多个同类型实体在各维度的表现。

    例如对比多个供应商的交付、定价和合规表现。

    Args:
        entity_type: 实体类型（如 Supplier）。
        entity_ids: 逗号分隔的实体 ID 列表。
        dimensions: 对比维度（如 "delivery,pricing,compliance"），为空则全维度对比。

    Returns:
        JSON 格式的对比结果。
    """
    client = _get_graphiti_client()
    ids = [eid.strip() for eid in entity_ids.split(",") if eid.strip()]

    comparisons: list[dict[str, Any]] = []
    for eid in ids:
        query = f"{entity_type} {eid} performance summary"
        if dimensions:
            query += f" {dimensions}"
        facts = await client.search(query, num_results=5)
        comparisons.append({
            "entity_id": eid,
            "entity_type": entity_type,
            "facts": facts,
        })

    return _clip_and_dump(comparisons)


# ── 6. detect_graph_anomalies ────────────────────────────────


@tool
async def detect_graph_anomalies(
    scope: str = "all",
    time_range_days: int = 30,
) -> str:
    """基于图结构检测采购流程中的异常模式。

    检测内容包括：无收货的采购订单、无关联PO的发票、孤立付款、循环引用等。

    Args:
        scope: 检测范围 - "all" / "po_without_receipt" / "invoice_without_po" /
               "orphan_payments" / "circular_references"。
        time_range_days: 检查时间范围（天）。

    Returns:
        JSON 格式的异常检测结果。
    """
    client = _get_graphiti_client()

    scope_queries = {
        "po_without_receipt": "purchase orders without receipts or delivery",
        "invoice_without_po": "invoices not linked to any purchase order",
        "orphan_payments": "payments not associated with any invoice",
        "circular_references": "circular references in procurement process",
    }

    if scope == "all":
        queries = list(scope_queries.values())
    else:
        queries = [scope_queries.get(scope, f"anomalies in {scope}")]

    anomalies: list[dict[str, Any]] = []
    for q in queries:
        results = await client.search(
            f"anomaly detection: {q} in last {time_range_days} days",
            num_results=10,
        )
        if results:
            anomalies.append({"check": q, "findings": results})

    return _clip_and_dump(anomalies)
