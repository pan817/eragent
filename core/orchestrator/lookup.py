"""DATA_LOOKUP 快捷路径。

对被识别为 DATA_LOOKUP 的查询，根据已提取的实体编号或 query 关键词
直接调用对应 query_* 工具，跳过 ReAct Agent，实现零 LLM 消耗的事实查询。

两条路径（共享 lookup_shortcut_enabled 开关）：
- 路径 A：有实体编号（po_number / invoice_num 等）→ 精确查询
- 路径 B：无编号但有实体类型关键词（"查最新PO"）→ 默认参数查询
- 两者均未命中 → 返回 None，由 orchestrator 降级到 ReAct
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


def resolve_lookup_tool(
    params: dict[str, Any],
    query: str,
) -> tuple[str, dict[str, Any]] | None:
    """根据实体/关键词确定应调用的工具和参数。

    Returns:
        (tool_name, tool_kwargs) 或 None（无法确定，应降级 ReAct）。
        特殊返回 tool_name="__supplier_combo__" 表示需要双工具调用。
    """
    days = params.get("days", 30)
    limit = params.get("limit", 0)
    order_by = params.get("order_by", "")

    # 从 params 中取 limit/order_by（统一 LLM 已提取）
    # _parse_query_constraints 作为兜底（LLM 未提取时）
    if not limit and not order_by:
        q_limit, q_order = _parse_query_constraints(query)
        if q_limit:
            limit = q_limit
        if q_order:
            order_by = q_order

    # 有 limit 意图时放宽 days
    if limit and days <= 30:
        days = 365

    # 注意：跨实体检测已由统一 LLM 的 is_cross_entity 判断，
    # 在 orchestrator 层拦截（不进入此函数）。

    def _with_constraints(base: dict[str, Any]) -> dict[str, Any]:
        """为 tool kwargs 注入 limit/order_by。"""
        if limit:
            base["limit"] = limit
        if order_by:
            base["order_by"] = order_by
        return base

    # 路径 A：有实体编号 → 不限时间（用户明确指定的实体可能创建于任何时间）
    for entity_field, tool_name, param_map in _ENTITY_TOOL_MAP:
        entity_val = params.get(entity_field)
        if entity_val:
            kwargs: dict[str, Any] = {"days": 0}
            for tool_param, source_field in param_map.items():
                kwargs[tool_param] = params[source_field]
            return tool_name, _with_constraints(kwargs)

    # 路径 A 特殊分支：vendor_id（双工具调用）→ 不限时间
    vendor_id = params.get("vendor_id")
    if vendor_id:
        return "__supplier_combo__", _with_constraints({
            "vendor_id": vendor_id,
            "days": 0,
        })

    # 路径 B：关键词推断
    from modules.p2p.intent_rules import LOOKUP_KEYWORD_TOOL_MAP

    q_lower = query.lower()
    for keywords, tool_name, defaults in LOOKUP_KEYWORD_TOOL_MAP:
        if any(kw in q_lower for kw in keywords):
            kwargs = {**defaults, "days": days}
            return tool_name, _with_constraints(kwargs)

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
        return format_lookup_result(
            result_json, tool_name,
            limit=tool_kwargs.get("limit", 0),
            order_by=tool_kwargs.get("order_by", ""),
        )
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

    return f"{vendor_md}\n\n{po_md}"


# ── 格式化：JSON → Markdown ──────────────────────────────────────────


# 各工具的表格列定义：(显示名, JSON 字段名)
_COLUMN_DEFS: dict[str, list[tuple[str, str]]] = {
    "query_purchase_orders": [
        ("PO 编号", "po_number"),
        ("供应商", "vendor_name"),
        ("金额", "total_amount"),
        ("状态", "status"),
        ("日期", "order_date"),
    ],
    "query_invoices": [
        ("发票号", "invoice_num"),
        ("PO 编号", "po_number"),
        ("金额", "total_amount"),
        ("状态", "status"),
        ("日期", "invoice_date"),
    ],
    "query_payments": [
        ("付款单号", "check_number"),
        ("发票号", "invoice_num"),
        ("金额", "amount"),
        ("状态", "status"),
        ("日期", "check_date"),
    ],
    "query_receipts": [
        ("收货单号", "receipt_number"),
        ("PO 编号", "po_number"),
        ("数量", "quantity"),
        ("状态", "status"),
        ("日期", "receipt_date"),
    ],
    "query_vendor_master": [
        ("供应商 ID", "vendor_id"),
        ("名称", "vendor_name"),
        ("站点", "site"),
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
