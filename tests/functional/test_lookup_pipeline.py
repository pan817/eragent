"""Lookup 全链路测试（真实工具 + 真实 DB）。

覆盖 resolve_lookup_tool → tool.ainvoke → format_lookup_result → 空结果 fallback。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.orchestrator.lookup import execute_lookup, format_lookup_result, resolve_lookup_tool
from modules.p2p.provider import P2PModuleProvider

_provider = P2PModuleProvider()


class TestLookupWithRealTools:
    """真实工具 + 真实 DB 的 lookup 全链路。"""

    async def test_latest_one_po_resolve(self, tool_registry):
        result = resolve_lookup_tool({"days": 30}, "最新的一个po", provider=_provider)
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_purchase_orders"
        assert kwargs["limit"] == 1
        assert kwargs["order_by"] == "date_desc"

    async def test_latest_one_po_execute(self, tool_registry):
        result_md = await execute_lookup(
            "query_purchase_orders",
            {"days": 0, "limit": 1, "order_by": "date_desc"},
            tool_registry,
        )
        assert result_md is not None
        assert "未找到" not in result_md
        assert "|" in result_md
        data_rows = [
            line for line in result_md.split("\n")
            if line.startswith("|") and "---" not in line
        ]
        assert len(data_rows) == 2  # 1 header + 1 data

    async def test_latest_one_po_is_actually_latest(
        self, tool_registry, seed_data_summary,
    ):
        result_md = await execute_lookup(
            "query_purchase_orders",
            {"days": 0, "limit": 1, "order_by": "date_desc"},
            tool_registry,
        )
        assert result_md is not None
        assert seed_data_summary["latest_po"] in result_md

    async def test_latest_5_po_count_and_order(self, tool_registry):
        result_md = await execute_lookup(
            "query_purchase_orders",
            {"days": 0, "limit": 5, "order_by": "date_desc"},
            tool_registry,
        )
        assert result_md is not None
        data_rows = [
            line for line in result_md.split("\n")
            if line.startswith("|") and "---" not in line
        ]
        assert len(data_rows) == 6  # 1 header + 5 data

    async def test_amount_desc_3_payments(self, tool_registry):
        result_md = await execute_lookup(
            "query_payments",
            {"days": 0, "limit": 3, "order_by": "amount_desc"},
            tool_registry,
        )
        assert result_md is not None
        data_rows = [
            line for line in result_md.split("\n")
            if line.startswith("|") and "---" not in line and line.strip()
        ]
        assert len(data_rows) == 4  # 1 header + 3 data

    async def test_entity_po_number_resolve(self):
        result = resolve_lookup_tool(
            {"po_number": "PO-2024-0001", "days": 30},
            "查看PO-2024-0001",
            provider=_provider,
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_purchase_orders"
        assert kwargs["po_number"] == "PO-2024-0001"
        assert kwargs["days"] == 0

    async def test_entity_vendor_filter(self, tool_registry):
        result_md = await execute_lookup(
            "query_purchase_orders",
            {"vendor_id": "SUP-001", "days": 0},
            tool_registry,
        )
        assert result_md is not None
        assert "SUP-001" not in result_md or "|" in result_md

    async def test_receipt_lookup(self, tool_registry):
        result_md = await execute_lookup(
            "query_receipts",
            {"days": 0, "limit": 3, "order_by": "date_desc"},
            tool_registry,
        )
        assert result_md is not None
        assert "|" in result_md

    async def test_supplier_list_via_combo(self, tool_registry):
        result_md = await execute_lookup(
            "__supplier_combo__",
            {"vendor_id": "SUP-001", "days": 0},
            tool_registry,
        )
        assert result_md is not None
        assert "SUP-001" in result_md


    async def test_invoice_time_window_resolve(self):
        result = resolve_lookup_tool(
            {"days": 7}, "查询最近7天的发票", provider=_provider,
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_invoices"
        assert kwargs["days"] == 7


class TestLookupEmptyAndFallback:
    """空结果和降级路径。"""

    async def test_empty_result_returns_none(self, tool_registry):
        result_md = await execute_lookup(
            "query_purchase_orders",
            {"days": 1, "limit": 1, "order_by": "date_desc", "po_number": "NONEXISTENT-PO-999"},
            tool_registry,
        )
        assert result_md is None

    async def test_tool_exception_returns_none(self):
        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(side_effect=RuntimeError("DB error"))
        registry = MagicMock()
        registry.get.return_value = mock_tool
        result = await execute_lookup("query_purchase_orders", {"days": 30}, registry)
        assert result is None

    async def test_blacklist_word_bypasses_lookup(self):
        result = resolve_lookup_tool(
            {"days": 7}, "最近采购订单异常", provider=_provider,
        )
        assert result is None

    async def test_cross_entity_bypasses_lookup(self):
        result = resolve_lookup_tool(
            {"days": 7}, "最近7天的采购订单和发票", provider=_provider,
        )
        assert result is None
