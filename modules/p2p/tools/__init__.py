"""P2P 工具包：按模式返回对应工具集。

- postgresql 模式：15 个 PG 工具（SQL 查询 + 规则检测 + 聚合分析）
- hybrid 模式：15 个 PG 工具 + 12 个 Graph 工具 = 27 个
- 跨模式通用：1 个 chat history 工具（recall 场景）
"""

from modules.p2p.tools._inject import (
    _get_graphiti_client,
    _get_query_backend,
    _get_repository,
    set_graphiti_client,
    set_query_backend,
    set_repository,
)
from modules.p2p.tools.pg import PG_TOOLS
from modules.p2p.tools.pg.query import (
    query_invoices,
    query_payments,
    query_purchase_orders,
    query_receipts,
)
from modules.p2p.tools.pg.analysis import (
    calculate_spend_analysis,
    calculate_supplier_kpis,
    query_vendor_master,
    run_payment_compliance_check,
    run_price_variance_analysis,
    run_three_way_match,
)
from modules.p2p.tools.pg.advanced import (
    analyze_discount_utilization,
    analyze_receipt_anomalies,
    analyze_vendor_concentration,
    calculate_po_cycle_time,
    detect_duplicate_invoices,
)
from modules.p2p.tools.graph import GRAPH_TOOLS
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
from modules.p2p.tools.chat_history import search_my_chat_history

CHAT_TOOLS = [search_my_chat_history]


def get_tools_for_mode(mode: str) -> list:
    """根据 query_backend 模式返回对应工具集。

    Args:
        mode: "postgresql"、"hybrid" 或 "graphiti"（向后兼容，等同 hybrid）。

    Returns:
        工具函数列表。postgresql 模式返回 16 个（15 PG + 1 chat），
        其余返回 28 个（15 PG + 12 Graph + 1 chat）。
    """
    if mode == "postgresql":
        return list(PG_TOOLS) + CHAT_TOOLS
    else:  # hybrid / graphiti (backward compat)
        return list(PG_TOOLS) + list(GRAPH_TOOLS) + CHAT_TOOLS


__all__ = [
    # inject
    "set_repository",
    "_get_repository",
    "set_graphiti_client",
    "_get_graphiti_client",
    "set_query_backend",
    "_get_query_backend",
    # mode-based tool set
    "get_tools_for_mode",
    "PG_TOOLS",
    "GRAPH_TOOLS",
    "CHAT_TOOLS",
    "search_my_chat_history",
]
