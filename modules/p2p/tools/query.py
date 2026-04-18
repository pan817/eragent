"""P2P 数据查询工具（4 个）。"""

from __future__ import annotations

import asyncio

from langchain.tools import tool

from modules.p2p.tools._inject import _get_repository
from modules.p2p.tools._output import _clip_and_dump


@tool
async def query_purchase_orders(
    supplier_id: str = "",
    status: str = "",
    po_number: str = "",
    days: int = 30,
) -> str:
    """查询采购订单数据。

    根据供应商 ID、订单状态、采购订单号等条件筛选采购订单列表。

    Args:
        supplier_id: 供应商 ID，为空则返回全部。
        status: 订单状态过滤，如 approved、pending，为空则不过滤。
        po_number: 采购订单号，为空则不按 PO 过滤。
        days: 查询最近 N 天内的订单，默认 30 天。

    Returns:
        JSON 格式的采购订单列表字符串。
    """
    repo = _get_repository()
    pos = await asyncio.to_thread(
        repo.query_purchase_orders,
        supplier_id=supplier_id,
        status=status,
        po_number=po_number,
        days=days,
    )
    return _clip_and_dump(pos)


@tool
async def query_receipts(
    po_number: str = "",
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """查询收货记录。

    根据采购订单号或供应商 ID 筛选收货记录。

    Args:
        po_number: 采购订单号，为空则不按 PO 过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        days: 查询最近 N 天内的记录，默认 30 天。

    Returns:
        JSON 格式的收货记录列表字符串。
    """
    repo = _get_repository()
    receipts = await asyncio.to_thread(
        repo.query_receipts,
        po_number=po_number,
        supplier_id=supplier_id,
        days=days,
    )
    return _clip_and_dump(receipts)


@tool
async def query_invoices(
    po_number: str = "",
    supplier_id: str = "",
    status: str = "",
    invoice_number: str = "",
    days: int = 30,
) -> str:
    """查询发票数据。

    根据采购订单号、供应商 ID、发票号或发票状态筛选发票记录。

    Args:
        po_number: 采购订单号，为空则不按 PO 过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        status: 发票状态过滤，如 pending、paid，为空则不过滤。
        invoice_number: 发票号，为空则不按发票过滤。
        days: 查询最近 N 天内的发票，默认 30 天。

    Returns:
        JSON 格式的发票列表字符串。
    """
    repo = _get_repository()
    invoices = await asyncio.to_thread(
        repo.query_invoices,
        po_number=po_number,
        supplier_id=supplier_id,
        status=status,
        invoice_number=invoice_number,
        days=days,
    )
    return _clip_and_dump(invoices)


@tool
async def query_payments(
    invoice_number: str = "",
    supplier_id: str = "",
    payment_number: str = "",
    days: int = 30,
) -> str:
    """查询付款记录。

    根据发票号、供应商 ID 或付款单号筛选付款记录。

    Args:
        invoice_number: 发票号，为空则不按发票过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        payment_number: 付款单号，为空则不按付款单过滤。
        days: 查询最近 N 天内的付款，默认 30 天。

    Returns:
        JSON 格式的付款记录列表字符串。
    """
    repo = _get_repository()
    payments = await asyncio.to_thread(
        repo.query_payments,
        invoice_number=invoice_number,
        supplier_id=supplier_id,
        payment_number=payment_number,
        days=days,
    )
    return _clip_and_dump(payments)
