"""PG 工具集（15个）：精确查询 + 规则检测 + 聚合分析。

postgresql 模式下全部使用 P2PRepository SQL 查询。
hybrid 模式下通过 QueryBackend 自动切换到 Neo4jStructuredBackend。
"""

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

PG_TOOLS = [
    # 精确查询（4个）
    query_purchase_orders,
    query_receipts,
    query_invoices,
    query_payments,
    # 规则检测（6个）
    run_three_way_match,
    run_price_variance_analysis,
    run_payment_compliance_check,
    calculate_supplier_kpis,
    query_vendor_master,
    calculate_spend_analysis,
    # 聚合分析（5个）
    analyze_receipt_anomalies,
    detect_duplicate_invoices,
    analyze_discount_utilization,
    analyze_vendor_concentration,
    calculate_po_cycle_time,
]

__all__ = [
    "PG_TOOLS",
    "query_purchase_orders",
    "query_receipts",
    "query_invoices",
    "query_payments",
    "run_three_way_match",
    "run_price_variance_analysis",
    "run_payment_compliance_check",
    "calculate_supplier_kpis",
    "query_vendor_master",
    "calculate_spend_analysis",
    "analyze_receipt_anomalies",
    "detect_duplicate_invoices",
    "analyze_discount_utilization",
    "analyze_vendor_concentration",
    "calculate_po_cycle_time",
]
