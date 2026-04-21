"""事件展示标签映射。

将 observability 侧的开发者标识符（tool/task 函数名、stage key）翻译成面向
最终用户的中文短语，供前端直接渲染。后端 observability 字段（``name`` /
``task_name``）保持不变，用于排障和 trace 回查；对外事件额外携带 ``label``
字段，前端优先使用。

新增工具/阶段时在下面的字典里补齐对应中文，未登记的 key 走
``_fallback_label`` 兜底并打 WARNING，提醒维护者及时登记。
"""

from __future__ import annotations

from core.logging_utils import get_logger

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 映射表
# ---------------------------------------------------------------------------

# Tool / DAG task（共用同一张表，因为 DAG task_name 即 tool registry key）
_TOOL_LABELS: dict[str, str] = {
    # 查询类
    "query_purchase_orders": "查询采购订单",
    "query_receipts": "查询收货记录",
    "query_goods_receipts": "查询收货记录",
    "query_invoices": "查询发票记录",
    "query_vendor_invoices": "查询供应商发票",
    "query_payments": "查询付款记录",
    "query_vendor_master": "查询供应商主数据",
    # 核对 / 合规
    "run_three_way_match": "三路匹配核对",
    "run_price_variance_analysis": "价格差异分析",
    "run_payment_compliance_check": "付款合规检查",
    # 指标 / 绩效
    "calculate_supplier_kpis": "供应商绩效测算",
    "calculate_spend_analysis": "采购支出分析",
    "calculate_po_cycle_time": "采购周期测算",
    # 异常分析
    "analyze_receipt_anomalies": "收货异常分析",
    "detect_duplicate_invoices": "重复发票检测",
    "analyze_discount_utilization": "折扣利用率分析",
    "analyze_vendor_concentration": "供应商集中度分析",
    # 图查询 — 语义搜索
    "search_knowledge_graph": "知识图谱搜索",
    # 图查询 — 实体操作
    "get_entity_detail": "查询实体详情",
    "query_entity_timeline": "查询实体时间线",
    "query_entity_relationships": "查询实体关系网络",
    # 图查询 — 路径遍历
    "find_path_between": "查询实体间路径",
    "trace_procurement_chain": "追踪采购全链路",
    "query_supplier_profile": "供应商画像分析",
    # 图查询 — 异常检测
    "detect_graph_anomalies": "图结构异常检测",
    "query_risk_impact": "风险影响面评估",
    # 图查询 — 对比分析
    "compare_entities": "多实体对比分析",
    "find_contract_coverage": "合同覆盖分析",
    "find_competing_suppliers": "竞争供应商分析",
    # 报告 / 图表
    "generate_summary_report": "生成分析报告",
    "generate_chart": "生成图表",
    # Agent 自身
    "agent": "智能体推理",
}


# Orchestrator 阶段事件
_STAGE_LABELS: dict[str, str] = {
    "intent_resolved": "意图识别完成",
    "dag_planned": "分析计划生成",
    "react_started": "开始智能分析",
    "lookup_shortcut": "快速查询",
}


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------


def _fallback_label(kind: str, name: str) -> str:
    """未登记时的兜底文案。记一条 WARNING 提醒补登记。"""
    _logger.warning(
        "display label missing: kind=%s name=%s (请在 display_labels.py 中补登记)",
        kind, name,
    )
    return f"处理中 · {name}"


def resolve_tool_label(name: str) -> str:
    """解析 tool / DAG task 的展示文案。

    支持 ``graph:xxx`` 前缀格式——先按原名查，再去掉前缀查。
    """
    label = _TOOL_LABELS.get(name)
    if label is not None:
        return label
    # Strip "graph:" or "pg:" prefix and try again
    for prefix in ("graph:", "pg:"):
        if name.startswith(prefix):
            bare = name[len(prefix):]
            label = _TOOL_LABELS.get(bare)
            if label is not None:
                return label
    return _fallback_label("tool", name)


def resolve_model_label(name: str) -> str:
    """解析 model span 的展示文案。

    model span 的 name 是动态模型名（如 ``qwen3-max``），
    无需静态映射，直接拼接通用前缀。
    """
    return f"LLM 推理 · {name}"


def resolve_stage_label(name: str) -> str:
    """解析 stage 事件的展示文案。"""
    label = _STAGE_LABELS.get(name)
    if label is not None:
        return label
    return _fallback_label("stage", name)
