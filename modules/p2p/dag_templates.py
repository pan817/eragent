"""P2P DAG 模板定义。

从 core/orchestrator/dag/templates.py 迁入的 P2P 专属 DAG 模板。
模板中的 {days} / {vendor_id} / {po_number} 占位符由加载时替换。
"""

from __future__ import annotations

from typing import Any

from api.schemas.domain import AnalysisType


# ── 三路匹配 DAG ────────────────────────────────────────────────────

_THREE_WAY_MATCH_DAG = [
    {
        "task_id": "t1",
        "name": "采集采购订单",
        "agent": "p2p_agent",
        "tool_name": "query_purchase_orders",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "po_number": "{po_number}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "采集收货记录",
        "agent": "p2p_agent",
        "tool_name": "query_receipts",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "po_number": "{po_number}"},
        "output_key": "gr_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "采集发票数据",
        "agent": "p2p_agent",
        "tool_name": "query_invoices",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "po_number": "{po_number}", "invoice_num": "{invoice_num}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t4",
        "name": "执行三路匹配检查",
        "agent": "p2p_agent",
        "tool_name": "run_three_way_match",
        "depends_on": ["t1", "t2", "t3"],
        "inputs": {"po_number": "{po_number}"},
        "output_key": "match_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t5",
        "name": "生成分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t4"],
        "inputs": {"scenario": "三路匹配分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 价格差异 DAG ────────────────────────────────────────────────────

_PRICE_VARIANCE_DAG = [
    {
        "task_id": "t1",
        "name": "采集采购订单",
        "agent": "p2p_agent",
        "tool_name": "query_purchase_orders",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "po_number": "{po_number}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "执行价格差异分析",
        "agent": "p2p_agent",
        "tool_name": "run_price_variance_analysis",
        "depends_on": ["t1"],
        "inputs": {"vendor_id": "{vendor_id}", "days": "{days}"},
        "output_key": "ppv_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t3",
        "name": "生成分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t2"],
        "inputs": {"scenario": "价格差异分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 付款合规 DAG ────────────────────────────────────────────────────

_PAYMENT_COMPLIANCE_DAG = [
    {
        "task_id": "t1",
        "name": "采集发票数据",
        "agent": "p2p_agent",
        "tool_name": "query_invoices",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "po_number": "{po_number}", "invoice_num": "{invoice_num}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "采集付款记录",
        "agent": "p2p_agent",
        "tool_name": "query_payments",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "check_number": "{check_number}", "invoice_num": "{invoice_num}"},
        "output_key": "payment_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "执行付款合规检查",
        "agent": "p2p_agent",
        "tool_name": "run_payment_compliance_check",
        "depends_on": ["t1", "t2"],
        "inputs": {"vendor_id": "{vendor_id}", "days": "{days}"},
        "output_key": "compliance_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t4",
        "name": "生成分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t3"],
        "inputs": {"scenario": "付款合规分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 供应商绩效 DAG ──────────────────────────────────────────────────

_SUPPLIER_PERFORMANCE_DAG = [
    {
        "task_id": "t1",
        "name": "查询供应商主数据",
        "agent": "vendor_agent",
        "tool_name": "query_vendor_master",
        "depends_on": [],
        "inputs": {"vendor_ids": "{vendor_id}"},
        "output_key": "vendor_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "计算供应商 KPI",
        "agent": "vendor_agent",
        "tool_name": "calculate_supplier_kpis",
        "depends_on": ["t1"],
        "inputs": {"vendor_id": "{vendor_id}", "period": "近{days}天"},
        "output_key": "kpi_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t3",
        "name": "生成分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t2"],
        "inputs": {"scenario": "供应商绩效分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── PO 综合风险分析 DAG ────────────────────────────────────────────
# 针对单个 PO 做多维度分析：三路匹配 + 价格差异 + 付款合规

_PO_RISK_DAG = [
    {
        "task_id": "t1",
        "name": "采集采购订单",
        "agent": "p2p_agent",
        "tool_name": "query_purchase_orders",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "po_number": "{po_number}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "采集收货记录",
        "agent": "p2p_agent",
        "tool_name": "query_receipts",
        "depends_on": [],
        "inputs": {"days": "{days}", "po_number": "{po_number}", "vendor_id": "{vendor_id}"},
        "output_key": "gr_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "采集发票数据",
        "agent": "p2p_agent",
        "tool_name": "query_invoices",
        "depends_on": [],
        "inputs": {"days": "{days}", "po_number": "{po_number}", "vendor_id": "{vendor_id}", "invoice_num": "{invoice_num}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t4",
        "name": "三路匹配检查",
        "agent": "p2p_agent",
        "tool_name": "run_three_way_match",
        "depends_on": ["t1", "t2", "t3"],
        "inputs": {"po_number": "{po_number}"},
        "output_key": "match_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t5",
        "name": "价格差异分析",
        "agent": "p2p_agent",
        "tool_name": "run_price_variance_analysis",
        "depends_on": ["t1"],
        "inputs": {"vendor_id": "{vendor_id}", "days": "{days}"},
        "output_key": "ppv_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t6",
        "name": "付款合规检查",
        "agent": "p2p_agent",
        "tool_name": "run_payment_compliance_check",
        "depends_on": ["t3"],
        "inputs": {"vendor_id": "{vendor_id}", "days": "{days}"},
        "output_key": "compliance_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t7",
        "name": "生成综合风险报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t4", "t5", "t6"],
        "inputs": {"scenario": "采购订单 {po_number} 综合风险分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 供应商综合分析 DAG ──────────────────────────────────────────────
# 针对单个供应商做全方位分析：绩效 + 价格差异 + 付款合规

_SUPPLIER_RISK_DAG = [
    {
        "task_id": "t1",
        "name": "查询供应商主数据",
        "agent": "vendor_agent",
        "tool_name": "query_vendor_master",
        "depends_on": [],
        "inputs": {"vendor_ids": "{vendor_id}"},
        "output_key": "vendor_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "计算供应商 KPI",
        "agent": "vendor_agent",
        "tool_name": "calculate_supplier_kpis",
        "depends_on": ["t1"],
        "inputs": {"vendor_id": "{vendor_id}", "period": "近{days}天"},
        "output_key": "kpi_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t3",
        "name": "价格差异分析",
        "agent": "p2p_agent",
        "tool_name": "run_price_variance_analysis",
        "depends_on": ["t1"],
        "inputs": {"vendor_id": "{vendor_id}", "days": "{days}"},
        "output_key": "ppv_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t4",
        "name": "付款合规检查",
        "agent": "p2p_agent",
        "tool_name": "run_payment_compliance_check",
        "depends_on": ["t1"],
        "inputs": {"vendor_id": "{vendor_id}", "days": "{days}"},
        "output_key": "compliance_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t5",
        "name": "生成供应商综合分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t2", "t3", "t4"],
        "inputs": {"scenario": "供应商 {vendor_id} 综合风险分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 单笔付款单合规分析 DAG ──────────────────────────────────────────
# 针对单笔付款单：查询付款 + 关联发票 + 合规检查

_PAYMENT_SINGLE_DAG = [
    {
        "task_id": "t1",
        "name": "查询付款记录",
        "agent": "p2p_agent",
        "tool_name": "query_payments",
        "depends_on": [],
        "inputs": {"check_number": "{check_number}", "invoice_num": "{invoice_num}", "vendor_id": "{vendor_id}", "days": "{days}"},
        "output_key": "payment_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "查询关联发票",
        "agent": "p2p_agent",
        "tool_name": "query_invoices",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "invoice_num": "{invoice_num}", "po_number": "{po_number}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "付款合规检查",
        "agent": "p2p_agent",
        "tool_name": "run_payment_compliance_check",
        "depends_on": ["t1", "t2"],
        "inputs": {"vendor_id": "{vendor_id}", "days": "{days}"},
        "output_key": "compliance_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t4",
        "name": "生成合规分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t3"],
        "inputs": {"scenario": "付款单 {check_number} 合规分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 单笔发票分析 DAG ────────────────────────────────────────────────
# 针对单笔发票：查询发票 + 关联 PO + 三路匹配 + 付款合规

_INVOICE_SINGLE_DAG = [
    {
        "task_id": "t1",
        "name": "查询发票数据",
        "agent": "p2p_agent",
        "tool_name": "query_invoices",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "invoice_num": "{invoice_num}", "po_number": "{po_number}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "查询关联采购订单",
        "agent": "p2p_agent",
        "tool_name": "query_purchase_orders",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "po_number": "{po_number}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "查询收货记录",
        "agent": "p2p_agent",
        "tool_name": "query_receipts",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "po_number": "{po_number}"},
        "output_key": "gr_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t4",
        "name": "三路匹配检查",
        "agent": "p2p_agent",
        "tool_name": "run_three_way_match",
        "depends_on": ["t1", "t2", "t3"],
        "inputs": {"po_number": "{po_number}"},
        "output_key": "match_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t5",
        "name": "生成发票分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t4"],
        "inputs": {"scenario": "发票 {invoice_num} 综合分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 采购支出分析 DAG ───────────────────────────────────────────────

_SPEND_ANALYSIS_DAG = [
    {
        "task_id": "t1",
        "name": "采集采购订单",
        "agent": "p2p_agent",
        "tool_name": "query_purchase_orders",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "按品类汇总支出",
        "agent": "p2p_agent",
        "tool_name": "calculate_spend_analysis",
        "depends_on": ["t1"],
        "inputs": {"group_by": "category", "days": "{days}"},
        "output_key": "spend_by_category",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "按供应商汇总支出",
        "agent": "p2p_agent",
        "tool_name": "calculate_spend_analysis",
        "depends_on": ["t1"],
        "inputs": {"group_by": "supplier", "days": "{days}"},
        "output_key": "spend_by_supplier",
        "timeout_sec": 60,
    },
    {
        "task_id": "t4",
        "name": "生成支出分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t2", "t3"],
        "inputs": {"scenario": "采购支出分析（近{days}天）"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 收货异常分析 DAG ───────────────────────────────────────────────

_RECEIPT_ANOMALY_DAG = [
    {
        "task_id": "t1",
        "name": "采集采购订单",
        "agent": "p2p_agent",
        "tool_name": "query_purchase_orders",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "po_number": "{po_number}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "采集收货记录",
        "agent": "p2p_agent",
        "tool_name": "query_receipts",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}", "po_number": "{po_number}"},
        "output_key": "gr_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "分析收货异常",
        "agent": "p2p_agent",
        "tool_name": "analyze_receipt_anomalies",
        "depends_on": ["t1", "t2"],
        "inputs": {"vendor_id": "{vendor_id}", "po_number": "{po_number}", "days": "{days}"},
        "output_key": "anomaly_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t4",
        "name": "生成收货异常报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t3"],
        "inputs": {"scenario": "收货异常分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 发票重复检测 DAG ───────────────────────────────────────────────

_INVOICE_DUPLICATE_DAG = [
    {
        "task_id": "t1",
        "name": "采集发票数据",
        "agent": "p2p_agent",
        "tool_name": "query_invoices",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "检测重复发票",
        "agent": "p2p_agent",
        "tool_name": "detect_duplicate_invoices",
        "depends_on": ["t1"],
        "inputs": {"vendor_id": "{vendor_id}", "days": "{days}"},
        "output_key": "duplicate_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t3",
        "name": "生成重复发票报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t2"],
        "inputs": {"scenario": "发票重复检测"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 折扣利用率分析 DAG ─────────────────────────────────────────────

_DISCOUNT_UTILIZATION_DAG = [
    {
        "task_id": "t1",
        "name": "采集发票数据",
        "agent": "p2p_agent",
        "tool_name": "query_invoices",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "采集付款记录",
        "agent": "p2p_agent",
        "tool_name": "query_payments",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}"},
        "output_key": "payment_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "分析折扣利用率",
        "agent": "p2p_agent",
        "tool_name": "analyze_discount_utilization",
        "depends_on": ["t1", "t2"],
        "inputs": {"vendor_id": "{vendor_id}", "days": "{days}"},
        "output_key": "discount_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t4",
        "name": "生成折扣利用率报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t3"],
        "inputs": {"scenario": "早付折扣利用率分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── PO 周期分析 DAG ────────────────────────────────────────────────

_PO_CYCLE_TIME_DAG = [
    {
        "task_id": "t1",
        "name": "采集采购订单",
        "agent": "p2p_agent",
        "tool_name": "query_purchase_orders",
        "depends_on": [],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "计算 PO 周期时间",
        "agent": "p2p_agent",
        "tool_name": "calculate_po_cycle_time",
        "depends_on": ["t1"],
        "inputs": {"days": "{days}", "vendor_id": "{vendor_id}"},
        "output_key": "cycle_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t3",
        "name": "生成周期分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t2"],
        "inputs": {"scenario": "采购订单全流程周期分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 供应商集中度分析 DAG ───────────────────────────────────────────

_VENDOR_CONCENTRATION_DAG = [
    {
        "task_id": "t1",
        "name": "采集采购订单",
        "agent": "p2p_agent",
        "tool_name": "query_purchase_orders",
        "depends_on": [],
        "inputs": {"days": "{days}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "分析供应商集中度",
        "agent": "p2p_agent",
        "tool_name": "analyze_vendor_concentration",
        "depends_on": ["t1"],
        "inputs": {"days": "{days}"},
        "output_key": "concentration_result",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "按供应商汇总支出",
        "agent": "p2p_agent",
        "tool_name": "calculate_spend_analysis",
        "depends_on": ["t1"],
        "inputs": {"group_by": "supplier", "days": "{days}"},
        "output_key": "spend_by_supplier",
        "timeout_sec": 60,
    },
    {
        "task_id": "t4",
        "name": "生成集中度分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t2", "t3"],
        "inputs": {"scenario": "供应商集中度与采购依赖风险分析"},
        "output_key": "report",
        "timeout_sec": 780,
    },
]


# ── 通用概览模板 ────────────────────────────────────────────────────

_RECENT_PROCUREMENT_HEALTH_DAG: list[dict[str, Any]] = [
    {
        "task_id": "t1_po_summary",
        "type": "tool",
        "tool_name": "query_purchase_orders",
        "inputs": {"days": "{days}", "vendor_id": ""},
        "depends_on": [],
        "timeout_sec": 60,
        "output_key": "po_summary",
    },
    {
        "task_id": "t2_anomaly_topn",
        "type": "tool",
        "tool_name": "rule_three_way_match",
        "inputs": {"days": "{days}"},
        "depends_on": [],
        "timeout_sec": 120,
        "output_key": "anomaly_topn",
    },
    {
        "task_id": "t3_supplier_topn",
        "type": "tool",
        "tool_name": "rule_supplier_performance",
        "inputs": {"days": "{days}", "vendor_id": ""},
        "depends_on": [],
        "timeout_sec": 120,
        "output_key": "supplier_topn",
    },
    {
        "task_id": "t4_report",
        "type": "tool",
        "tool_name": "generate_summary_report",
        "inputs": {"scenario": "近{days}天采购健康度概览"},
        "depends_on": ["t1_po_summary", "t2_anomaly_topn", "t3_supplier_topn"],
        "timeout_sec": 120,
        "output_key": "report",
    },
]


# ── 模板注册表 ──────────────────────────────────────────────────────

# 分析类型维度模板
_TEMPLATE_MAP: dict[AnalysisType, list[dict[str, Any]]] = {
    AnalysisType.THREE_WAY_MATCH: _THREE_WAY_MATCH_DAG,
    AnalysisType.PRICE_VARIANCE: _PRICE_VARIANCE_DAG,
    AnalysisType.PAYMENT_COMPLIANCE: _PAYMENT_COMPLIANCE_DAG,
    AnalysisType.SUPPLIER_PERFORMANCE: _SUPPLIER_PERFORMANCE_DAG,
    AnalysisType.SPEND_ANALYSIS: _SPEND_ANALYSIS_DAG,
    AnalysisType.RECEIPT_ANOMALY: _RECEIPT_ANOMALY_DAG,
    AnalysisType.INVOICE_DUPLICATE: _INVOICE_DUPLICATE_DAG,
    AnalysisType.DISCOUNT_UTILIZATION: _DISCOUNT_UTILIZATION_DAG,
    AnalysisType.PO_CYCLE_TIME: _PO_CYCLE_TIME_DAG,
    AnalysisType.VENDOR_CONCENTRATION: _VENDOR_CONCENTRATION_DAG,
}

# 实体维度模板（当有特定实体 + 综合/风险分析意图时使用）
# 优先级：payment > invoice > po > supplier
_ENTITY_TEMPLATE_MAP: dict[str, list[dict[str, Any]]] = {
    "payment_single": _PAYMENT_SINGLE_DAG,
    "invoice_single": _INVOICE_SINGLE_DAG,
    "po_risk": _PO_RISK_DAG,
    "supplier_risk": _SUPPLIER_RISK_DAG,
}


# 通用概览模板（不按 AnalysisType 索引，按命名键检索）
_GENERIC_TEMPLATE_MAP: dict[str, list[dict[str, Any]]] = {
    "recent_procurement_health": _RECENT_PROCUREMENT_HEALTH_DAG,
}


def get_template_map() -> dict[AnalysisType, list[dict[str, Any]]]:
    """返回分析类型维度模板映射。"""
    return dict(_TEMPLATE_MAP)


def get_entity_template_map() -> dict[str, list[dict[str, Any]]]:
    """返回实体维度模板映射。"""
    return dict(_ENTITY_TEMPLATE_MAP)


def get_generic_template_map() -> dict[str, list[dict[str, Any]]]:
    """返回通用概览模板映射。"""
    return dict(_GENERIC_TEMPLATE_MAP)
