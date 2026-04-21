"""Graph 工具集（12个）：语义搜索 + 实体操作 + 路径遍历 + 异常检测 + 对比分析。

仅在 hybrid 模式下注入，通过 Cypher 查询 Neo4j 图结构。
"""

from modules.p2p.tools.graph.search import search_knowledge_graph
from modules.p2p.tools.graph.entity import (
    get_entity_detail,
    query_entity_relationships,
    query_entity_timeline,
)
from modules.p2p.tools.graph.traversal import (
    find_path_between,
    query_supplier_profile,
    trace_procurement_chain,
)
from modules.p2p.tools.graph.anomaly import (
    detect_graph_anomalies,
    query_risk_impact,
)
from modules.p2p.tools.graph.comparison import (
    compare_entities,
    find_competing_suppliers,
    find_contract_coverage,
)

GRAPH_TOOLS = [
    # 语义搜索（1个）
    search_knowledge_graph,
    # 实体操作（3个）
    get_entity_detail,
    query_entity_timeline,
    query_entity_relationships,
    # 路径遍历（3个）
    find_path_between,
    trace_procurement_chain,
    query_supplier_profile,
    # 异常检测（2个）
    detect_graph_anomalies,
    query_risk_impact,
    # 对比分析（3个）
    compare_entities,
    find_contract_coverage,
    find_competing_suppliers,
]

__all__ = [
    "GRAPH_TOOLS",
    "search_knowledge_graph",
    "get_entity_detail",
    "query_entity_timeline",
    "query_entity_relationships",
    "find_path_between",
    "trace_procurement_chain",
    "query_supplier_profile",
    "detect_graph_anomalies",
    "query_risk_impact",
    "compare_entities",
    "find_contract_coverage",
    "find_competing_suppliers",
]
