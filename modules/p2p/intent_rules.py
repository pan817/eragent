"""P2P 模块意图路由规则。

提供给 IntentRouter 的 P2P 特有关键词、实体、角色描述等。
通过 P2PModuleProvider 注入。
"""

from __future__ import annotations

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
    "付款", "付款单", "payment",
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
