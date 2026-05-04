"""标准查询集 + 预期路由映射。

所有测试共享的"查询 → 预期行为"映射，避免各文件各自硬编码查询字符串。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CanaryQuery:
    """一条金丝雀查询的完整预期。"""

    query: str
    expected_route: str
    expected_intent: str
    expected_tool: str | None
    min_result_count: int | None
    must_contain_fields: list[str] | None
    assert_order_field: str | None
    assert_order_dir: str | None
    tags: tuple[str, ...]


CANARY_QUERIES: list[CanaryQuery] = [
    # ── lookup shortcut 路径（8 条）──
    CanaryQuery(
        "最新的一个po", "lookup_shortcut", "DATA_LOOKUP",
        "query_purchase_orders", 1, ["po_number"],
        "creation_date", "desc", ("lookup", "po"),
    ),
    CanaryQuery(
        "最新的5个PO", "lookup_shortcut", "DATA_LOOKUP",
        "query_purchase_orders", 5, ["po_number"],
        "creation_date", "desc", ("lookup", "po"),
    ),
    CanaryQuery(
        "金额最大的3笔付款", "lookup_shortcut", "DATA_LOOKUP",
        "query_payments", 3, ["amount"],
        "amount", "desc", ("lookup", "payment"),
    ),
    CanaryQuery(
        "查询最近7天的发票", "lookup_shortcut", "DATA_LOOKUP",
        "query_invoices", 1, ["invoice_num"],
        None, None, ("lookup", "invoice"),
    ),
    CanaryQuery(
        "查看PO-001", "lookup_shortcut", "DATA_LOOKUP",
        "query_purchase_orders", 1, ["po_number"],
        None, None, ("lookup", "po", "entity"),
    ),
    CanaryQuery(
        "SUP-001的采购订单", "lookup_shortcut", "DATA_LOOKUP",
        "query_purchase_orders", 1, ["vendor_id"],
        None, None, ("lookup", "po", "entity"),
    ),
    CanaryQuery(
        "本月的供应商列表", "lookup_shortcut", "DATA_LOOKUP",
        "query_vendor_master", 1, [],
        None, None, ("lookup", "supplier"),
    ),
    CanaryQuery(
        "最新的3张收货单", "lookup_shortcut", "DATA_LOOKUP",
        "query_receipts", 1, [],
        None, None, ("lookup", "receipt"),
    ),
    # ── DAG 路径（4 条）──
    CanaryQuery(
        "分析三路匹配异常", "DAG", "ANALYSIS",
        "run_three_way_match", None, None, None, None, ("dag", "3wm"),
    ),
    CanaryQuery(
        "分析采购价格差异", "DAG", "ANALYSIS",
        "run_price_variance_analysis", None, None, None, None, ("dag", "price"),
    ),
    CanaryQuery(
        "检查付款合规性", "DAG", "ANALYSIS",
        "run_payment_compliance_check", None, None, None, None, ("dag", "compliance"),
    ),
    CanaryQuery(
        "评估SUP-001的绩效", "DAG", "ANALYSIS",
        "calculate_supplier_kpis", None, None, None, None, ("dag", "supplier"),
    ),
    # ── PS / agent 路径（4 条）──
    CanaryQuery(
        "帮我全面分析采购数据", "plan_and_solve|agent", "ANALYSIS",
        None, None, None, None, None, ("ps",),
    ),
    CanaryQuery(
        "查询最近7天采购订单概况", "plan_and_solve|agent", "ANALYSIS",
        None, None, None, None, None, ("ps",),
    ),
    CanaryQuery(
        "列出最近7天的采购订单和发票", "plan_and_solve|agent", "ANALYSIS",
        None, None, None, None, None, ("ps", "cross_entity"),
    ),
    CanaryQuery(
        "为什么最新的PO金额这么高", "plan_and_solve|agent", "ANALYSIS",
        None, None, None, None, None, ("ps", "blacklist"),
    ),
    # ── bypass 路径（4 条）──
    CanaryQuery(
        "你好", "bypass", "CHITCHAT",
        None, None, None, None, None, ("bypass",),
    ),
    CanaryQuery(
        "你能做什么", "bypass", "META",
        None, None, None, None, None, ("bypass",),
    ),
    CanaryQuery(
        "上次分析了什么", "bypass", "RECALL",
        None, None, None, None, None, ("bypass",),
    ),
    CanaryQuery(
        "最近付款合规情况", "DAG|plan_and_solve", "ANALYSIS",
        None, None, None, None, None, ("blacklist",),
    ),
]


def get_queries_by_tag(*tags: str) -> list[CanaryQuery]:
    """返回含任一指定 tag 的查询子集。"""
    tag_set = set(tags)
    return [q for q in CANARY_QUERIES if tag_set & set(q.tags)]
