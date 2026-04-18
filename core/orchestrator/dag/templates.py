"""
静态 DAG 模板。

为四种核心分析类型各定义一个预置 DAG，Level 1/2 命中后直接加载执行。
模板中的 {days} / {supplier_id} / {po_number} 占位符由加载时替换。
"""

from __future__ import annotations

from typing import Any

from api.schemas.domain import AnalysisType


def _replace_params(tasks: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    """将 DAG 模板中的参数占位符替换为实际值。

    支持两种占位符：
    - 纯占位符："{days}" → 直接替换为 params["days"]
    - 嵌入占位符："近{days}天" → 字符串内插替换
    """
    import copy
    import re
    result = []
    for task in tasks:
        t = copy.deepcopy(task)
        inputs = t.get("inputs", {})
        for key, val in inputs.items():
            if not isinstance(val, str) or "{" not in val:
                continue
            if val.startswith("{") and val.endswith("}") and val.count("{") == 1:
                # 纯占位符
                param_name = val[1:-1]
                inputs[key] = params.get(param_name, val)
            else:
                # 嵌入占位符：用正则替换所有 {xxx}
                def _sub(m: re.Match) -> str:
                    return str(params.get(m.group(1), m.group(0)))
                inputs[key] = re.sub(r"\{(\w+)\}", _sub, val)
        result.append(t)
    return result


# ── 三路匹配 DAG ────────────────────────────────────────────────────

_THREE_WAY_MATCH_DAG = [
    {
        "task_id": "t1",
        "name": "采集采购订单",
        "agent": "p2p_agent",
        "tool_name": "query_purchase_orders",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "po_number": "{po_number}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "采集收货记录",
        "agent": "p2p_agent",
        "tool_name": "query_receipts",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "po_number": "{po_number}"},
        "output_key": "gr_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "采集发票数据",
        "agent": "p2p_agent",
        "tool_name": "query_invoices",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "po_number": "{po_number}", "invoice_number": "{invoice_number}"},
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "po_number": "{po_number}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "执行价格差异分析",
        "agent": "p2p_agent",
        "tool_name": "run_price_variance_analysis",
        "depends_on": ["t1"],
        "inputs": {"supplier_id": "{supplier_id}", "days": "{days}"},
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "po_number": "{po_number}", "invoice_number": "{invoice_number}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "采集付款记录",
        "agent": "p2p_agent",
        "tool_name": "query_payments",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "payment_number": "{payment_number}", "invoice_number": "{invoice_number}"},
        "output_key": "payment_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "执行付款合规检查",
        "agent": "p2p_agent",
        "tool_name": "run_payment_compliance_check",
        "depends_on": ["t1", "t2"],
        "inputs": {"supplier_id": "{supplier_id}", "days": "{days}"},
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
        "inputs": {"vendor_ids": "{supplier_id}"},
        "output_key": "vendor_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "计算供应商 KPI",
        "agent": "vendor_agent",
        "tool_name": "calculate_supplier_kpis",
        "depends_on": ["t1"],
        "inputs": {"supplier_id": "{supplier_id}", "period": "近{days}天"},
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "po_number": "{po_number}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "采集收货记录",
        "agent": "p2p_agent",
        "tool_name": "query_receipts",
        "depends_on": [],
        "inputs": {"days": "{days}", "po_number": "{po_number}", "supplier_id": "{supplier_id}"},
        "output_key": "gr_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "采集发票数据",
        "agent": "p2p_agent",
        "tool_name": "query_invoices",
        "depends_on": [],
        "inputs": {"days": "{days}", "po_number": "{po_number}", "supplier_id": "{supplier_id}", "invoice_number": "{invoice_number}"},
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
        "inputs": {"supplier_id": "{supplier_id}", "days": "{days}"},
        "output_key": "ppv_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t6",
        "name": "付款合规检查",
        "agent": "p2p_agent",
        "tool_name": "run_payment_compliance_check",
        "depends_on": ["t3"],
        "inputs": {"supplier_id": "{supplier_id}", "days": "{days}"},
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
        "inputs": {"vendor_ids": "{supplier_id}"},
        "output_key": "vendor_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "计算供应商 KPI",
        "agent": "vendor_agent",
        "tool_name": "calculate_supplier_kpis",
        "depends_on": ["t1"],
        "inputs": {"supplier_id": "{supplier_id}", "period": "近{days}天"},
        "output_key": "kpi_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t3",
        "name": "价格差异分析",
        "agent": "p2p_agent",
        "tool_name": "run_price_variance_analysis",
        "depends_on": ["t1"],
        "inputs": {"supplier_id": "{supplier_id}", "days": "{days}"},
        "output_key": "ppv_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t4",
        "name": "付款合规检查",
        "agent": "p2p_agent",
        "tool_name": "run_payment_compliance_check",
        "depends_on": ["t1"],
        "inputs": {"supplier_id": "{supplier_id}", "days": "{days}"},
        "output_key": "compliance_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t5",
        "name": "生成供应商综合分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t2", "t3", "t4"],
        "inputs": {"scenario": "供应商 {supplier_id} 综合风险分析"},
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
        "inputs": {"payment_number": "{payment_number}", "invoice_number": "{invoice_number}", "supplier_id": "{supplier_id}", "days": "{days}"},
        "output_key": "payment_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "查询关联发票",
        "agent": "p2p_agent",
        "tool_name": "query_invoices",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "invoice_number": "{invoice_number}", "po_number": "{po_number}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "付款合规检查",
        "agent": "p2p_agent",
        "tool_name": "run_payment_compliance_check",
        "depends_on": ["t1", "t2"],
        "inputs": {"supplier_id": "{supplier_id}", "days": "{days}"},
        "output_key": "compliance_result",
        "timeout_sec": 780,
    },
    {
        "task_id": "t4",
        "name": "生成合规分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t3"],
        "inputs": {"scenario": "付款单 {payment_number} 合规分析"},
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "invoice_number": "{invoice_number}", "po_number": "{po_number}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "查询关联采购订单",
        "agent": "p2p_agent",
        "tool_name": "query_purchase_orders",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "po_number": "{po_number}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "查询收货记录",
        "agent": "p2p_agent",
        "tool_name": "query_receipts",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "po_number": "{po_number}"},
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
        "inputs": {"scenario": "发票 {invoice_number} 综合分析"},
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "po_number": "{po_number}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "采集收货记录",
        "agent": "p2p_agent",
        "tool_name": "query_receipts",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}", "po_number": "{po_number}"},
        "output_key": "gr_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "分析收货异常",
        "agent": "p2p_agent",
        "tool_name": "analyze_receipt_anomalies",
        "depends_on": ["t1", "t2"],
        "inputs": {"supplier_id": "{supplier_id}", "po_number": "{po_number}", "days": "{days}"},
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "检测重复发票",
        "agent": "p2p_agent",
        "tool_name": "detect_duplicate_invoices",
        "depends_on": ["t1"],
        "inputs": {"supplier_id": "{supplier_id}", "days": "{days}"},
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
        "output_key": "invoice_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "采集付款记录",
        "agent": "p2p_agent",
        "tool_name": "query_payments",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
        "output_key": "payment_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "分析折扣利用率",
        "agent": "p2p_agent",
        "tool_name": "analyze_discount_utilization",
        "depends_on": ["t1", "t2"],
        "inputs": {"supplier_id": "{supplier_id}", "days": "{days}"},
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
        "output_key": "po_data",
        "timeout_sec": 60,
    },
    {
        "task_id": "t2",
        "name": "计算 PO 周期时间",
        "agent": "p2p_agent",
        "tool_name": "calculate_po_cycle_time",
        "depends_on": ["t1"],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
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


def load_dag_template(
    analysis_type: AnalysisType,
    params: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """加载并参数化 DAG 模板。

    选择逻辑（优先级从高到低）：
    1. COMPREHENSIVE + payment_number → 单笔付款单合规 DAG
    2. COMPREHENSIVE + invoice_number → 单笔发票分析 DAG
    3. COMPREHENSIVE + po_number → PO 综合风险 DAG
    4. COMPREHENSIVE + supplier_id → 供应商综合分析 DAG
    5. 按 analysis_type 匹配分析类型维度模板
    6. 无匹配 → 返回 None（由调用方降级到 ReAct）

    Args:
        analysis_type: 分析类型。
        params: 参数字典。

    Returns:
        参数化后的 DAG 任务列表，无匹配模板时返回 None。
    """
    effective_params = {
        "days": params.get("days", 30),
        "supplier_id": params.get("supplier_id", ""),
        "po_number": params.get("po_number", ""),
        "invoice_number": params.get("invoice_number", ""),
        "payment_number": params.get("payment_number", ""),
        "receipt_number": params.get("receipt_number", ""),
    }

    # 实体维度模板优先：有具体实体 + COMPREHENSIVE 意图
    if analysis_type == AnalysisType.COMPREHENSIVE:
        # 按单据层级优先级：付款 > 发票 > PO > 供应商
        if effective_params["payment_number"]:
            template = _ENTITY_TEMPLATE_MAP["payment_single"]
            return _replace_params(template, effective_params)
        if effective_params["invoice_number"]:
            template = _ENTITY_TEMPLATE_MAP["invoice_single"]
            return _replace_params(template, effective_params)
        if effective_params["po_number"]:
            template = _ENTITY_TEMPLATE_MAP["po_risk"]
            return _replace_params(template, effective_params)
        if effective_params["supplier_id"]:
            template = _ENTITY_TEMPLATE_MAP["supplier_risk"]
            return _replace_params(template, effective_params)

    # 分析类型维度模板
    template = _TEMPLATE_MAP.get(analysis_type)
    if template is None:
        return None

    return _replace_params(template, effective_params)
