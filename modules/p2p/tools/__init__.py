"""P2P 工具包：统一导出全部 25 个 @tool + 注入管理。"""

from modules.p2p.tools._inject import (
    _get_graphiti_client,
    _get_query_backend,
    _get_repository,
    set_graphiti_client,
    set_query_backend,
    set_repository,
)
from modules.p2p.tools.query import (
    query_invoices,
    query_payments,
    query_purchase_orders,
    query_receipts,
)
from modules.p2p.tools.analysis import (
    calculate_spend_analysis,
    calculate_supplier_kpis,
    query_vendor_master,
    run_payment_compliance_check,
    run_price_variance_analysis,
    run_three_way_match,
)
from modules.p2p.tools.advanced import (
    analyze_discount_utilization,
    analyze_receipt_anomalies,
    analyze_vendor_concentration,
    calculate_po_cycle_time,
    detect_duplicate_invoices,
)
from modules.p2p.tools.stub import (
    check_approval_limits,
    check_blacklist,
    query_material_master,
    run_vendor_risk_scoring,
)
from modules.p2p.tools.graph import (
    compare_entities,
    detect_graph_anomalies,
    query_entity_relationships,
    query_entity_timeline,
    query_supplier_profile,
    search_knowledge_graph,
)

__all__ = [
    # inject
    "set_repository",
    "_get_repository",
    "set_graphiti_client",
    "_get_graphiti_client",
    "set_query_backend",
    "_get_query_backend",
    # query (4)
    "query_purchase_orders",
    "query_receipts",
    "query_invoices",
    "query_payments",
    # analysis (6)
    "run_three_way_match",
    "run_price_variance_analysis",
    "run_payment_compliance_check",
    "calculate_supplier_kpis",
    "query_vendor_master",
    "calculate_spend_analysis",
    # advanced (5)
    "analyze_receipt_anomalies",
    "detect_duplicate_invoices",
    "analyze_discount_utilization",
    "analyze_vendor_concentration",
    "calculate_po_cycle_time",
    # stub (4)
    "query_material_master",
    "run_vendor_risk_scoring",
    "check_approval_limits",
    "check_blacklist",
    # graph (6)
    "search_knowledge_graph",
    "query_entity_timeline",
    "query_entity_relationships",
    "query_supplier_profile",
    "compare_entities",
    "detect_graph_anomalies",
]
