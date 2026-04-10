"""
静态 DAG 模板。

为四种核心分析类型各定义一个预置 DAG，Level 1/2 命中后直接加载执行。
模板中的 {days} / {supplier_id} / {po_number} 占位符由加载时替换。
"""

from __future__ import annotations

from typing import Any

from api.schemas.analysis import AnalysisType


def _replace_params(tasks: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    """将 DAG 模板中的参数占位符替换为实际值。"""
    import copy
    result = []
    for task in tasks:
        t = copy.deepcopy(task)
        inputs = t.get("inputs", {})
        for key, val in inputs.items():
            if isinstance(val, str) and val.startswith("{") and val.endswith("}"):
                param_name = val[1:-1]
                inputs[key] = params.get(param_name, val)
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
        "output_key": "po_data",
        "timeout_sec": 30,
    },
    {
        "task_id": "t2",
        "name": "采集收货记录",
        "agent": "p2p_agent",
        "tool_name": "query_receipts",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
        "output_key": "gr_data",
        "timeout_sec": 30,
    },
    {
        "task_id": "t3",
        "name": "采集发票数据",
        "agent": "p2p_agent",
        "tool_name": "query_invoices",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
        "output_key": "invoice_data",
        "timeout_sec": 30,
    },
    {
        "task_id": "t4",
        "name": "执行三路匹配检查",
        "agent": "p2p_agent",
        "tool_name": "run_three_way_match",
        "depends_on": ["t1", "t2", "t3"],
        "inputs": {"po_number": "{po_number}"},
        "output_key": "match_result",
        "timeout_sec": 60,
    },
    {
        "task_id": "t5",
        "name": "生成分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t4"],
        "inputs": {"scenario": "三路匹配分析"},
        "output_key": "report",
        "timeout_sec": 60,
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
        "output_key": "po_data",
        "timeout_sec": 30,
    },
    {
        "task_id": "t2",
        "name": "执行价格差异分析",
        "agent": "p2p_agent",
        "tool_name": "run_price_variance_analysis",
        "depends_on": ["t1"],
        "inputs": {"supplier_id": "{supplier_id}", "days": "{days}"},
        "output_key": "ppv_result",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "生成分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t2"],
        "inputs": {"scenario": "价格差异分析"},
        "output_key": "report",
        "timeout_sec": 60,
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
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
        "output_key": "invoice_data",
        "timeout_sec": 30,
    },
    {
        "task_id": "t2",
        "name": "采集付款记录",
        "agent": "p2p_agent",
        "tool_name": "query_payments",
        "depends_on": [],
        "inputs": {"days": "{days}", "supplier_id": "{supplier_id}"},
        "output_key": "payment_data",
        "timeout_sec": 30,
    },
    {
        "task_id": "t3",
        "name": "执行付款合规检查",
        "agent": "p2p_agent",
        "tool_name": "run_payment_compliance_check",
        "depends_on": ["t1", "t2"],
        "inputs": {"supplier_id": "{supplier_id}", "days": "{days}"},
        "output_key": "compliance_result",
        "timeout_sec": 60,
    },
    {
        "task_id": "t4",
        "name": "生成分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t3"],
        "inputs": {"scenario": "付款合规分析"},
        "output_key": "report",
        "timeout_sec": 60,
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
        "timeout_sec": 30,
    },
    {
        "task_id": "t2",
        "name": "计算供应商 KPI",
        "agent": "vendor_agent",
        "tool_name": "calculate_supplier_kpis",
        "depends_on": ["t1"],
        "inputs": {"supplier_id": "{supplier_id}", "period": "近{days}天"},
        "output_key": "kpi_result",
        "timeout_sec": 60,
    },
    {
        "task_id": "t3",
        "name": "生成分析报告",
        "agent": "report_agent",
        "tool_name": "generate_summary_report",
        "depends_on": ["t2"],
        "inputs": {"scenario": "供应商绩效分析"},
        "output_key": "report",
        "timeout_sec": 60,
    },
]


# ── 模板注册表 ──────────────────────────────────────────────────────

_TEMPLATE_MAP: dict[AnalysisType, list[dict[str, Any]]] = {
    AnalysisType.THREE_WAY_MATCH: _THREE_WAY_MATCH_DAG,
    AnalysisType.PRICE_VARIANCE: _PRICE_VARIANCE_DAG,
    AnalysisType.PAYMENT_COMPLIANCE: _PAYMENT_COMPLIANCE_DAG,
    AnalysisType.SUPPLIER_PERFORMANCE: _SUPPLIER_PERFORMANCE_DAG,
}


def load_dag_template(
    analysis_type: AnalysisType,
    params: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """加载并参数化 DAG 模板。

    Args:
        analysis_type: 分析类型。
        params: 参数字典，可包含 days / supplier_id / po_number。

    Returns:
        参数化后的 DAG 任务列表，无匹配模板时返回 None。
    """
    template = _TEMPLATE_MAP.get(analysis_type)
    if template is None:
        return None

    # 设置默认值
    effective_params = {
        "days": params.get("days", 30),
        "supplier_id": params.get("supplier_id", ""),
        "po_number": params.get("po_number", ""),
    }

    return _replace_params(template, effective_params)
