"""P2P 模块意图路由规则。

提供给 IntentRouter 的 P2P 特有关键词、实体、角色描述等。
通过 P2PModuleProvider 注入。
"""

from __future__ import annotations

# L3 LLM 分类 prompt 中的 analysis_type 枚举描述
ANALYSIS_TYPE_DESCRIPTIONS: list[tuple[str, str]] = [
    ("three_way_match", "采购订单、收货单、发票的三单匹配异常检查"),
    ("price_variance", "实际采购价格与合同价/标准价的偏差分析"),
    ("payment_compliance", "付款逾期、提前付款、折扣滥用等合规检查；也覆盖\"应付未付/该付没付/欠款/到期未付\"类查询"),
    ("supplier_performance", "供应商交期、质量、KPI 综合绩效评估"),
    ("spend_analysis", "按品类/供应商维度的采购支出分布分析"),
    ("receipt_anomaly", "超量收货、拒收、延迟收货等收货异常分析"),
    ("invoice_duplicate", "重复发票检测"),
    ("discount_utilization", "早付折扣利用率分析"),
    ("po_cycle_time", "采购订单各环节耗时/时间周期统计（从创建到收货到付款的时间间隔）；注意：\"追踪链路/关系追溯/上下游关联\"不属于此类，应归 comprehensive"),
    ("vendor_concentration", "供应商集中度与采购依赖风险分析"),
    ("comprehensive", "明确需要跨多个维度组合分析（如\"综合评估供应商风险\"），或链路追踪/关系追溯/上下游关联查询（如\"追踪PO的完整采购链路\"\"查看发票对应的收货和付款\"）"),
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

# lookup 快捷路径高置信度关键词映射（四重漏斗之漏斗 #1 "实体类型唯一"）
# 每条目：(实体类型词集合, 工具名)
# 相比老版本的 LOOKUP_KEYWORD_TOOL_MAP，刻意剔除：
#   - 过短易误匹配的简写（inv / rcv / gr / sup）
#   - 业务术语但语义模糊（应付 / 支付 / 付款记录）
# 命中规则：query 中有且仅有一组关键词命中（多类别 → miss）。
LOOKUP_HIGH_CONFIDENCE_KEYWORDS: list[tuple[set[str], str]] = [
    ({"po", "po号", "采购单", "采购订单"}, "query_purchase_orders"),
    ({"发票", "invoice"}, "query_invoices"),
    ({"付款", "付款单", "payment", "支付单"}, "query_payments"),
    ({"收货", "收货单", "receipt"}, "query_receipts"),
    ({"供应商", "supplier"}, "query_vendor_master"),
]

# lookup 黑名单词（四重漏斗之漏斗 #3 "无分析/诊断/概览意图"）
# query 含任一词 → 判定为综合分析意图，lookup miss 交给 PS。
# 覆盖三类意图：
#   - 分析/诊断：异常 / 差异 / 为什么 / 怎么样 / 是否 / 分析 / 对比 / 评估 / 审查 / 原因 / 影响
#   - 综合概览：概况 / 概览 / 情况 / 健康 / 趋势
#   - 风险合规：风险 / 合规 / 绩效
LOOKUP_EXCLUSION_WORDS: set[str] = {
    "异常", "差异", "为什么", "怎么样", "是否",
    "概况", "概览", "情况", "分析", "对比", "评估", "审查",
    "健康", "风险", "合规", "绩效", "趋势", "原因", "影响",
}

# lookup 关系追溯词（四重漏斗之漏斗 #4 "无关系追溯意图"）
# query 含任一词 → 可能需要图查询工具，lookup miss 交给 PS/ReAct。
# 与 orchestrator._matches_graph_query 的词集保持一致，但只保留与 lookup 冲突的核心词。
LOOKUP_GRAPH_INTENT_WORDS: set[str] = {
    "链路", "链条", "关联", "关系", "上下游", "追踪", "追溯", "溯源", "路径",
}


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
