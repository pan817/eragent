"""DATA_LOOKUP 快捷路径。

两条路径（共享 lookup_shortcut_enabled 开关）：

- **路径 A：实体编号精确查询**
  有 po_number / invoice_num / check_number / vendor_id → 直调工具。
  实体编号 = 100% 确定性信号，零 LLM 消耗 + 零误中风险。

- **路径 B：高置信度关键词映射**（四重漏斗）
  query 同时满足四个信号才命中，任一缺失放行到 Plan and Solve：
    #1 实体类型词唯一（仅含"PO/发票/付款/收货/供应商"中一类，不含多类）
    #2 有数量/排序修饰 或 明确时间窗（"最新 5 个"/"金额最大 3 笔"/"最近 7 天"）
    #3 不含分析/诊断/概览黑名单词（异常/概况/对比/为什么/健康/风险…）
    #4 不含关系追溯词（链路/关联/追踪/上下游…）

路径 B 的历史：早期版本仅凭"query 含采购订单/发票"等单一关键词即命中，
贪心匹配把"最近 7 天采购订单概况"这类综合分析查询误拦成"列表表格"。
引入四重漏斗后，规则严格到近似"只在极明确的事实列表查询时才命中"，
误中概率显著降低；覆盖面窄于老版本，但大于"只保留路径 A"的极简方案。
"""

from __future__ import annotations

import json
from typing import Any

from core.logging_utils import get_logger

_logger = get_logger(__name__)

# ── 从 query 中解析数量/排序意图 ────────────────────────────────

import re

# "最新的一个" / "最近3条" / "前5个" 等
_LIMIT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # "最新/最近的N个/条/笔"
    (re.compile(r"(?:最新|最近|最后)\s*(?:的\s*)?(\d+)\s*(?:个|条|笔|张)", re.I), "date_desc"),
    # "前N个/条"
    (re.compile(r"前\s*(\d+)\s*(?:个|条|笔|张)", re.I), "date_desc"),
    # "最新/最近的一个" (无数字，隐含 1)
    (re.compile(r"(?:最新|最近|最后)\s*(?:的\s*)?(?:一\s*)?(?:个|条|笔|张)", re.I), "date_desc"),
    # "最早的N个"
    (re.compile(r"最早\s*(?:的\s*)?(\d+)\s*(?:个|条|笔|张)", re.I), "date_asc"),
    (re.compile(r"最早\s*(?:的\s*)?(?:一\s*)?(?:个|条|笔|张)", re.I), "date_asc"),
    # "金额最大/最高的N个"
    (re.compile(r"(?:金额|总额)\s*(?:最大|最高)\s*(?:的\s*)?(\d+)\s*(?:个|条|笔)", re.I), "amount_desc"),
    (re.compile(r"(?:金额|总额)\s*(?:最大|最高)\s*(?:的\s*)?(?:一\s*)?(?:个|条|笔)", re.I), "amount_desc"),
]


def _parse_query_constraints(query: str) -> tuple[int, str]:
    """从 query 中解析 limit 和 order_by。

    Returns:
        (limit, order_by)。未匹配时返回 (0, "")。
    """
    for pattern, default_order in _LIMIT_PATTERNS:
        m = pattern.search(query)
        if m:
            # 有数字捕获组则取值，否则默认 1
            try:
                limit = int(m.group(1))
            except (IndexError, TypeError):
                limit = 1
            return limit, default_order
    return 0, ""


# 路径 A：实体编号 → (工具名, 参数映射)
# 优先级按列表顺序，首个命中即返回
_ENTITY_TOOL_MAP: list[tuple[str, str, dict[str, str]]] = [
    # (实体字段, 工具名, {工具参数名: 实体字段名})
    ("po_number", "query_purchase_orders", {"po_number": "po_number"}),
    ("invoice_num", "query_invoices", {"invoice_num": "invoice_num"}),
    ("check_number", "query_payments", {"check_number": "check_number"}),
    # receipt_number 不支持直调（query_receipts 无此参数），跳过
    # vendor_id 需特殊处理（双工具调用），见 _resolve_supplier_lookup
]


# ── 路径 B 四重漏斗：时间窗识别 ───────────────────────────────────

# "最近 N 天/月/周"/"过去 N 天"/"本月"等明确时间窗
_EXPLICIT_TIME_WINDOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:最近|过去|近)\s*\d+\s*(?:天|月|周|年|日)", re.I),
    re.compile(r"(?:past|last|recent)\s+\d+\s*(?:day|week|month|year)s?", re.I),
    re.compile(r"本(?:月|周|年)|上(?:月|周|年)|今(?:天|日|月|年)", re.I),
]


def _has_explicit_time_window(query: str) -> bool:
    """检测 query 是否包含明确时间窗（用户显式给定）。"""
    return any(p.search(query) for p in _EXPLICIT_TIME_WINDOW_PATTERNS)


def _resolve_high_confidence_keyword_tool(
    query: str,
    limit: int,
    order_by: str,
    lookup_rules: dict[str, Any] | None = None,
) -> str | None:
    """路径 B 四重漏斗：判断是否可按关键词映射到工具。

    四个漏斗同时满足才返回工具名，任一缺失返回 None 交给 PS。

    Args:
        query: 用户查询文本。
        limit: 已解析的数量约束（0 表示未指定）。
        order_by: 已解析的排序约束（"" 表示未指定）。
        lookup_rules: provider.get_lookup_rules() 返回的规则字典。

    Returns:
        工具名或 None。
    """
    if lookup_rules is None:
        return None

    LOOKUP_HIGH_CONFIDENCE_KEYWORDS = lookup_rules["high_confidence_keywords"]
    LOOKUP_EXCLUSION_WORDS = lookup_rules["exclusion_words"]
    LOOKUP_GRAPH_INTENT_WORDS = lookup_rules["graph_intent_words"]

    q_lower = query.lower()

    # 漏斗 #3：黑名单词（分析/诊断/概览意图）→ miss
    if any(w in q_lower for w in LOOKUP_EXCLUSION_WORDS):
        return None

    # 漏斗 #4：关系追溯词 → miss（交给 PS/ReAct 调图查询）
    if any(w in q_lower for w in LOOKUP_GRAPH_INTENT_WORDS):
        return None

    # 漏斗 #1：实体类型词唯一（命中多类别或零类别都 miss）
    matched_tool: str | None = None
    for keywords, tool_name in LOOKUP_HIGH_CONFIDENCE_KEYWORDS:
        if any(kw in q_lower for kw in keywords):
            if matched_tool is not None:
                return None  # 多类别 → miss
            matched_tool = tool_name
    if matched_tool is None:
        return None  # 无实体类型词 → miss

    # 漏斗 #2：数量/排序修饰 或 明确时间窗
    if not (limit or order_by or _has_explicit_time_window(query)):
        return None

    return matched_tool


def resolve_lookup_tool(
    params: dict[str, Any],
    query: str,
    provider: Any = None,
) -> tuple[str, dict[str, Any]] | None:
    """根据实体编号或高置信度关键词确定应调用的工具和参数。

    两条路径（见模块 docstring）：
      - 路径 A：params 含实体编号 → 直接返回工具映射（确定性最高）
      - 路径 B：query 通过四重漏斗 → 按关键词映射（高置信度）
      - 两者都不满足 → 返回 None，由 orchestrator 进入 Plan and Solve

    Returns:
        (tool_name, tool_kwargs) 或 None。
        特殊返回 tool_name="__supplier_combo__" 表示需要双工具调用。
    """
    limit = params.get("limit", 0)
    order_by = params.get("order_by", "")

    # 从 query 解析数量/排序兜底（LLM 未提取时）
    if not limit and not order_by:
        q_limit, q_order = _parse_query_constraints(query)
        if q_limit:
            limit = q_limit
        if q_order:
            order_by = q_order

    # 注意：跨实体检测已由统一 LLM 的 is_cross_entity 判断，
    # 在 orchestrator 层拦截（不进入此函数）。

    def _with_constraints(base: dict[str, Any]) -> dict[str, Any]:
        """为 tool kwargs 注入 limit/order_by。"""
        if limit:
            base["limit"] = limit
        if order_by:
            base["order_by"] = order_by
        return base

    # ── 路径 A：有实体编号 → 直调（不限时间）────────────────────
    for entity_field, tool_name, param_map in _ENTITY_TOOL_MAP:
        entity_val = params.get(entity_field)
        if entity_val:
            kwargs: dict[str, Any] = {"days": 0}
            for tool_param, source_field in param_map.items():
                kwargs[tool_param] = params[source_field]
            return tool_name, _with_constraints(kwargs)

    # vendor_id 特殊分支（双工具调用）→ 不限时间
    vendor_id = params.get("vendor_id")
    if vendor_id:
        return "__supplier_combo__", _with_constraints({
            "vendor_id": vendor_id,
            "days": 0,
        })

    # ── 路径 B：四重漏斗高置信度关键词映射 ─────────────────────
    lookup_rules = provider.get_lookup_rules() if provider else None
    hc_tool = _resolve_high_confidence_keyword_tool(query, limit, order_by, lookup_rules)
    if hc_tool is not None:
        # 命中 → days 沿用 orchestrator 已决策的值；0 表示不限时间（合法值）
        days_val = params.get("days")
        kwargs = {"days": days_val if days_val is not None else 30}
        return hc_tool, _with_constraints(kwargs)

    # 两条路径都未命中 → 交给 Plan and Solve
    return None


async def execute_lookup(
    tool_name: str,
    tool_kwargs: dict[str, Any],
    registry: Any,
) -> str | None:
    """执行快捷路径工具调用。

    Args:
        tool_name: 工具名或 "__supplier_combo__"（双工具）。
        tool_kwargs: 工具参数。
        registry: ToolRegistry 实例。

    Returns:
        格式化后的 Markdown 字符串，或 None（工具未注册/调用失败）。
    """
    try:
        if tool_name == "__supplier_combo__":
            return await _execute_supplier_combo(tool_kwargs, registry)

        tool_fn = registry.get(tool_name)
        if tool_fn is None:
            _logger.warning("lookup shortcut: tool '%s' not registered", tool_name)
            return None

        result_json = await tool_fn.ainvoke(tool_kwargs)
        result_md = format_lookup_result(
            result_json, tool_name,
            limit=tool_kwargs.get("limit", 0),
            order_by=tool_kwargs.get("order_by", ""),
        )
        if result_md.startswith("未找到"):
            _logger.info("lookup shortcut: empty result for tool=%s", tool_name)
            return None
        return result_md
    except Exception as exc:
        _logger.warning(
            "lookup shortcut execution failed: tool=%s error=%s",
            tool_name, exc, exc_info=True,
        )
        return None


async def _execute_supplier_combo(
    kwargs: dict[str, Any],
    registry: Any,
) -> str | None:
    """vendor_id 双工具调用：vendor_master + purchase_orders。"""
    import asyncio

    vendor_id = kwargs["vendor_id"]
    days = kwargs.get("days", 30)
    limit = kwargs.get("limit", 0)
    order_by = kwargs.get("order_by", "")

    vendor_fn = registry.get("query_vendor_master")
    po_fn = registry.get("query_purchase_orders")

    if vendor_fn is None or po_fn is None:
        _logger.warning("lookup shortcut: supplier combo tools not registered")
        return None

    po_kwargs: dict[str, Any] = {"vendor_id": vendor_id, "days": days}
    if limit:
        po_kwargs["limit"] = limit
    if order_by:
        po_kwargs["order_by"] = order_by

    vendor_task = vendor_fn.ainvoke({"vendor_ids": vendor_id})
    po_task = po_fn.ainvoke(po_kwargs)
    vendor_json, po_json = await asyncio.gather(vendor_task, po_task)

    vendor_md = format_lookup_result(vendor_json, "query_vendor_master")
    po_md = format_lookup_result(
        po_json, "query_purchase_orders",
        limit=limit, order_by=order_by,
    )

    vendor_empty = vendor_md.startswith("未找到")
    po_empty = po_md.startswith("未找到")
    if vendor_empty and po_empty:
        return None

    return f"{vendor_md}\n\n{po_md}"


# ── 格式化：JSON → Markdown ──────────────────────────────────────────


# 各工具的表格列定义：(显示名, JSON 字段名)
# 字段名必须与 P2PRepository / Neo4jStructuredBackend 实际返回的 dict key 一致
_COLUMN_DEFS: dict[str, list[tuple[str, str]]] = {
    "query_purchase_orders": [
        ("PO 编号", "po_number"),
        ("供应商", "vendor_name"),
        ("金额", "po_amount"),
        ("状态", "status"),
        ("日期", "creation_date"),
    ],
    "query_invoices": [
        ("发票号", "invoice_num"),
        ("PO 编号", "po_number"),
        ("金额", "invoice_amount"),
        ("状态", "approval_status"),
        ("日期", "creation_date"),
    ],
    "query_payments": [
        ("付款单号", "check_number"),
        ("发票号", "invoice_num"),
        ("金额", "amount"),
        ("付款方式", "payment_method_code"),
        ("日期", "check_date"),
    ],
    "query_receipts": [
        ("收货单号", "gr_number"),
        ("PO 编号", "po_number"),
        ("数量", "gr_quantity"),
        ("质检", "quality_passed"),
        ("日期", "receipt_date"),
    ],
    "query_vendor_master": [
        ("供应商 ID", "vendor_id"),
        ("名称", "vendor_name"),
        ("编码", "segment1"),
        ("付款条款", "terms_id"),
        ("状态", "enabled_flag"),
    ],
}

# 工具名 → 中文实体名
_TOOL_ENTITY_NAMES: dict[str, str] = {
    "query_purchase_orders": "采购订单",
    "query_invoices": "发票",
    "query_payments": "付款记录",
    "query_receipts": "收货记录",
    "query_vendor_master": "供应商",
}


def format_lookup_result(
    result_json: str,
    tool_name: str,
    *,
    limit: int = 0,
    order_by: str = "",
) -> str:
    """将 query 工具返回的 JSON 字符串格式化为 Markdown 表格 + 摘要。

    Args:
        result_json: query_* 工具返回的 JSON 字符串。
        tool_name: 工具名（用于确定表格列）。
        limit: 查询时使用的数量限制（用于生成更精确的摘要）。
        order_by: 查询时使用的排序方式（用于生成更精确的摘要）。

    Returns:
        Markdown 格式字符串。
    """
    try:
        data = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return f"查询结果：\n\n```\n{result_json[:500]}\n```"

    # 处理截断标记
    truncated_count = 0
    if isinstance(data, list):
        items = [
            item for item in data
            if not (isinstance(item, dict) and item.get("_truncated"))
        ]
        truncated_items = [
            item for item in data
            if isinstance(item, dict) and item.get("_truncated")
        ]
        if truncated_items:
            truncated_count = truncated_items[0].get("dropped", 0)
    elif isinstance(data, dict):
        items = [data]
    else:
        return f"查询结果：\n\n```\n{result_json[:500]}\n```"

    if not items:
        entity_name = _TOOL_ENTITY_NAMES.get(tool_name, "记录")
        return f"未找到匹配的{entity_name}。"

    columns = _COLUMN_DEFS.get(tool_name)
    if columns is None:
        return f"查询到 {len(items)} 条记录。\n\n```json\n{result_json[:800]}\n```"

    # 构建 Markdown 表格
    header = " | ".join(col[0] for col in columns)
    separator = " | ".join("---" for _ in columns)
    rows: list[str] = []
    for item in items:
        cells = [str(item.get(col[1], "-")) for col in columns]
        rows.append(" | ".join(cells))

    entity_name = _TOOL_ENTITY_NAMES.get(tool_name, "记录")
    total_count = len(items) + truncated_count

    # 有 limit 约束时使用更精确的摘要
    _ORDER_LABELS = {
        "date_desc": "最新",
        "date_asc": "最早",
        "amount_desc": "金额最大",
        "amount_asc": "金额最小",
    }
    if limit > 0 and order_by in _ORDER_LABELS:
        order_label = _ORDER_LABELS[order_by]
        summary = f"{order_label}的 {len(items)} 条{entity_name}。"
    elif limit > 0:
        summary = f"共 {len(items)} 条{entity_name}。"
    else:
        summary = f"共 {total_count} 条{entity_name}"
        if truncated_count > 0:
            summary += f"（展示前 {len(items)} 条）"
        summary += "。"

    table = f"| {header} |\n| {separator} |\n"
    table += "\n".join(f"| {row} |" for row in rows)

    return f"{summary}\n\n{table}"
