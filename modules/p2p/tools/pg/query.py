"""PG 精确查询工具（4个）。

通过 QueryBackend 获取数据：
- postgresql 模式 → PostgreSQLBackend（Repository SQL）
- hybrid 模式 → Neo4jStructuredBackend（Cypher 节点查询 + 边关系增强）
"""

from __future__ import annotations

from langchain.tools import tool

from modules.p2p.tools._inject import _get_query_backend
from modules.p2p.tools._output import _clip_and_dump


@tool
async def query_purchase_orders(
    vendor_id: str = "",
    status: str = "",
    po_number: str = "",
    days: int = 30,
    limit: int = 0,
    order_by: str = "",
) -> str:
    """按条件精确查询采购订单明细数据。

    适用场景：需要获取具体的采购订单记录，支持按供应商、状态、订单号、时间范围精确过滤。
    返回内容：PO 行级明细（单价、数量、金额、状态、交期），可用于规则检测和数值分析。
    不适用：查询实体关联关系请用 query_entity_relationships；模糊搜索请用 search_knowledge_graph。

    Args:
        vendor_id: 供应商 ID（如 "V001"），为空则返回全部供应商的订单。
        status: 订单状态（approved/pending/closed），为空则不过滤。
        po_number: 采购订单号（如 "PO-001"），精确匹配。
        days: 查询最近 N 天内的订单，默认 30 天，0 表示不限时间范围。
        limit: 返回结果数量上限，0 表示不限制。
        order_by: 排序方式（date_desc/date_asc/amount_desc/amount_asc），为空则不排序。

    Returns:
        JSON 格式的采购订单列表字符串。
    """
    backend = _get_query_backend()
    pos = await backend.query_purchase_orders(
        vendor_id=vendor_id,
        status=status,
        po_number=po_number,
        days=days,
        limit=limit,
        order_by=order_by,
    )
    return _clip_and_dump(pos)


@tool
async def query_receipts(
    po_number: str = "",
    vendor_id: str = "",
    days: int = 30,
    limit: int = 0,
    order_by: str = "",
) -> str:
    """按条件精确查询收货记录。

    适用场景：需要获取具体的收货事务记录，按采购订单号或供应商过滤。
    返回内容：收货明细（数量、日期、质量状态），可用于三路匹配和收货异常分析。
    不适用：查询 PO 到收货的完整链路请用 trace_procurement_chain。

    Args:
        po_number: 采购订单号，为空则不按 PO 过滤。
        vendor_id: 供应商 ID，为空则不按供应商过滤。
        days: 查询最近 N 天内的记录，默认 30 天，0 表示不限时间范围。
        limit: 返回结果数量上限，0 表示不限制。
        order_by: 排序方式（date_desc/date_asc），为空则不排序。

    Returns:
        JSON 格式的收货记录列表字符串。
    """
    backend = _get_query_backend()
    receipts = await backend.query_receipts(
        po_number=po_number,
        vendor_id=vendor_id,
        days=days,
        limit=limit,
        order_by=order_by,
    )
    return _clip_and_dump(receipts)


@tool
async def query_invoices(
    po_number: str = "",
    vendor_id: str = "",
    status: str = "",
    invoice_num: str = "",
    days: int = 30,
    limit: int = 0,
    order_by: str = "",
) -> str:
    """按条件精确查询发票数据。

    适用场景：需要获取具体的发票记录，按订单号、供应商、发票号或状态精确过滤。
    返回内容：发票明细（金额、到期日、折扣截止日、审批状态），可用于付款合规和折扣分析。
    不适用：检测重复发票请用 detect_duplicate_invoices；查看发票关联关系请用 query_entity_relationships。

    Args:
        po_number: 采购订单号，为空则不按 PO 过滤。
        vendor_id: 供应商 ID，为空则不按供应商过滤。
        status: 发票状态（pending/approved/paid），为空则不过滤。
        invoice_num: 发票号（如 "INV-001"），精确匹配。
        days: 查询最近 N 天内的发票，默认 30 天，0 表示不限时间范围。
        limit: 返回结果数量上限，0 表示不限制。
        order_by: 排序方式（date_desc/date_asc/amount_desc/amount_asc），为空则不排序。

    Returns:
        JSON 格式的发票列表字符串。
    """
    backend = _get_query_backend()
    invoices = await backend.query_invoices(
        po_number=po_number,
        vendor_id=vendor_id,
        status=status,
        invoice_num=invoice_num,
        days=days,
        limit=limit,
        order_by=order_by,
    )
    return _clip_and_dump(invoices)


@tool
async def query_payments(
    invoice_num: str = "",
    vendor_id: str = "",
    check_number: str = "",
    days: int = 30,
    limit: int = 0,
    order_by: str = "",
) -> str:
    """按条件精确查询付款记录。

    适用场景：需要获取具体的付款事务记录，按发票号、供应商或付款单号过滤。
    返回内容：付款明细（金额、付款日期、付款方式），可用于付款合规检查。
    不适用：检测孤立付款请用 detect_graph_anomalies(scope="orphan_payment")。

    Args:
        invoice_num: 发票号，为空则不按发票过滤。
        vendor_id: 供应商 ID，为空则不按供应商过滤。
        check_number: 付款单号，为空则不按付款单过滤。
        days: 查询最近 N 天内的付款，默认 30 天，0 表示不限时间范围。
        limit: 返回结果数量上限，0 表示不限制。
        order_by: 排序方式（date_desc/date_asc/amount_desc/amount_asc），为空则不排序。

    Returns:
        JSON 格式的付款记录列表字符串。
    """
    backend = _get_query_backend()
    payments = await backend.query_payments(
        invoice_num=invoice_num,
        vendor_id=vendor_id,
        check_number=check_number,
        days=days,
        limit=limit,
        order_by=order_by,
    )
    return _clip_and_dump(payments)
