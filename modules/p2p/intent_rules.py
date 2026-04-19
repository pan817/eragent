"""P2P 模块意图路由规则。

提供给 IntentRouter 的 P2P 特有关键词、实体、角色描述等。
通过 P2PModuleProvider 注入。
"""

from __future__ import annotations

# L3 LLM 分类 prompt 中的 analysis_type 枚举描述
ANALYSIS_TYPE_DESCRIPTIONS: list[tuple[str, str]] = [
    ("three_way_match", "采购订单、收货单、发票的三单匹配异常检查"),
    ("price_variance", "实际采购价格与合同价/标准价的偏差分析"),
    ("payment_compliance", "付款逾期、提前付款、折扣滥用等合规检查"),
    ("supplier_performance", "供应商交期、质量、KPI 综合绩效评估"),
    ("spend_analysis", "按品类/供应商维度的采购支出分布分析"),
    ("receipt_anomaly", "超量收货、拒收、延迟收货等收货异常分析"),
    ("invoice_duplicate", "重复发票检测"),
    ("discount_utilization", "早付折扣利用率分析"),
    ("po_cycle_time", "采购订单全流程周期分析"),
    ("vendor_concentration", "供应商集中度与采购依赖风险分析"),
    ("comprehensive", "明确需要跨多个维度组合分析（如\"综合评估供应商风险\"）"),
]

# L1 分析关键词（出现这些词说明用户有具体业务意图）
ANALYSIS_KEYWORDS: set[str] = {
    # 分析动词
    "分析", "检查", "查看", "查询", "评估", "计算", "统计", "对比", "比较", "审查",
    "analyze", "check", "review", "evaluate", "calculate",
    # 数据对象
    "订单", "采购", "供应商", "发票", "付款", "收货", "价格", "三路匹配",
    "po", "supplier", "invoice", "payment", "receipt",
    # 时间范围修饰
    "天", "月", "年", "季度", "周",
    "days", "months", "month", "year", "week",
    # 业务关键词
    "异常", "差异", "逾期", "合规", "绩效", "kpi", "支出", "折扣",
    "重复", "周期", "集中度", "风险",
}

# L1 lookup 动词
LOOKUP_VERBS: set[str] = {
    "查", "查询", "查看", "查一下", "看下", "看看", "列出", "列一下", "拉一下",
    "显示", "show", "list", "get", "find", "fetch", "lookup", "search",
}

# L1 lookup 业务实体
LOOKUP_ENTITIES: set[str] = {
    "po", "po号", "采购单", "采购订单", "订单",
    "发票", "invoice", "inv",
    "付款", "付款单", "payment", "支付单", "支付", "付款记录", "应付",
    "收货", "收货单", "receipt", "rcv", "gr",
    "供应商", "supplier", "sup",
    "合同", "contract",
    "物料", "material", "item",
}

# lookup 修饰词
LOOKUP_MODIFIERS: set[str] = {
    "最新", "最近的", "最后", "最后一", "全部", "所有", "前", "后",
    "latest", "recent", "newest", "all", "first", "last",
}

# DATA_LOOKUP 关键词 → query 工具映射（路径 B：无实体编号时按关键词推断工具）
# 每个条目：(关键词集合, 工具名, 默认参数)
# 匹配优先级按列表顺序，首个命中即返回
LOOKUP_KEYWORD_TOOL_MAP: list[tuple[set[str], str, dict[str, str | int]]] = [
    (
        {"po", "po号", "采购单", "采购订单"},
        "query_purchase_orders",
        {"days": 30},
    ),
    (
        {"发票", "invoice", "inv"},
        "query_invoices",
        {"days": 30},
    ),
    (
        {"付款", "付款单", "payment", "支付单", "支付", "付款记录", "应付"},
        "query_payments",
        {"days": 30},
    ),
    (
        {"收货", "收货单", "receipt", "rcv", "gr"},
        "query_receipts",
        {"days": 30},
    ),
    (
        {"供应商", "supplier", "sup"},
        "query_vendor_master",
        {},
    ),
]


# L3 分析师角色描述
ROLE_DESCRIPTIONS: dict[str, str] = {
    "general": "",
    "procurement": (
        "采购分析师，重点关注采购订单、价格差异、供应商选择、"
        "采购支出和三路匹配等采购执行层面的问题。"
    ),
    "finance": (
        "财务合规人员，重点关注付款逾期、折扣利用率、发票重复、"
        "付款合规和资金风险等财务层面的问题。"
    ),
    "supply_chain": (
        "供应链经理，重点关注供应商绩效、交期、收货异常、"
        "供应商集中度和采购周期等供应链层面的问题。"
    ),
    "audit": (
        "审计人员，重点关注发票重复、付款合规、三路匹配异常、"
        "折扣滥用等合规审计层面的问题。"
    ),
    "management": (
        "管理层，重点关注采购支出趋势、供应商集中度、"
        "供应商绩效全局概览等战略层面的问题。"
    ),
}
