"""DATA_LOOKUP 快捷路径单元测试。

覆盖场景：
- resolve_lookup_tool：有实体编号命中工具 / 无实体编号返回 None（交给 PS）
- format_lookup_result：各实体类型的 Markdown 格式化
- execute_lookup：工具调用成功 / 工具未注册 / 调用异常

注：历史上的"路径 B 关键词推断"已废弃（含"采购订单/发票"等词会误拦
本应由 PS 综合分析的查询），resolve_lookup_tool 在无实体编号时统一返回 None。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.orchestrator.lookup import (
    _parse_query_constraints,
    execute_lookup,
    format_lookup_result,
    resolve_lookup_tool,
)


# ── resolve_lookup_tool 测试 ──────────────────────────────────────────


class TestResolveLookupTool:
    """resolve_lookup_tool 路由逻辑测试。"""

    def test_path_a_po_number(self) -> None:
        """路径 A：有 po_number → query_purchase_orders，days=0（不限时间）。"""
        result = resolve_lookup_tool(
            {"po_number": "PO-2024-001", "days": 30}, "查看PO-2024-001"
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_purchase_orders"
        assert kwargs["po_number"] == "PO-2024-001"
        assert kwargs["days"] == 0  # 有实体编号时不限时间

    def test_path_a_invoice_number(self) -> None:
        """路径 A：有 invoice_number → query_invoices，days=0（不限时间）。"""
        result = resolve_lookup_tool(
            {"invoice_num": "INV-001", "days": 7}, "查看发票INV-001"
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_invoices"
        assert kwargs["invoice_num"] == "INV-001"
        assert kwargs["days"] == 0  # 有实体编号时不限时间

    def test_path_a_payment_number(self) -> None:
        """路径 A：有 payment_number → query_payments。"""
        result = resolve_lookup_tool(
            {"check_number": "PAY-001", "days": 30}, "查看付款单PAY-001"
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_payments"
        assert kwargs["check_number"] == "PAY-001"

    def test_path_a_supplier_id(self) -> None:
        """路径 A：仅有 supplier_id → __supplier_combo__。"""
        result = resolve_lookup_tool(
            {"vendor_id": "SUP-001", "days": 30}, "查看供应商SUP-001"
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "__supplier_combo__"
        assert kwargs["vendor_id"] == "SUP-001"

    def test_path_a_priority_po_over_supplier(self) -> None:
        """路径 A：同时有 po_number 和 supplier_id 时优先 po_number。"""
        result = resolve_lookup_tool(
            {"po_number": "PO-001", "vendor_id": "SUP-001", "days": 30},
            "查看PO-001",
        )
        assert result is not None
        tool_name, _ = result
        assert tool_name == "query_purchase_orders"

    def test_no_entity_id_returns_none_even_with_keywords(self) -> None:
        """无实体编号时，即使 query 含"采购订单/发票"等词也应返回 None（交给 PS）。

        回归保护：历史上曾因"路径 B 关键词推断"把这类查询误拦成 lookup。
        """
        for q in (
            "查最新的采购订单",
            "列出最近的发票",
            "看看最近的付款记录",
            "查最近的收货单",
            "查看供应商列表",
            "最近 7 天的采购订单概况",
        ):
            assert resolve_lookup_tool({"days": 30}, q) is None, q

    def test_limit_and_order_by_passthrough_with_entity(self) -> None:
        """有实体编号时 limit/order_by 仍正确透传。"""
        result = resolve_lookup_tool(
            {"po_number": "PO-001", "days": 30, "limit": 1, "order_by": "date_desc"},
            "查看最新的一个PO",
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_purchase_orders"
        assert kwargs["limit"] == 1
        assert kwargs["order_by"] == "date_desc"

    def test_no_match_returns_none(self) -> None:
        """无实体无法命中 → None（由 orchestrator 进入 PS 或 ReAct）。"""
        result = resolve_lookup_tool({"days": 30}, "帮我查一下")
        assert result is None

    def test_receipt_number_not_supported(self) -> None:
        """receipt_number 未纳入 lookup 实体映射表，且无其他实体 → None。"""
        result = resolve_lookup_tool(
            {"receipt_number": "RCV-001", "days": 30}, "查看收货单RCV-001"
        )
        assert result is None


# ── _parse_query_constraints 测试 ─────────────────────────────────────


class TestParseQueryConstraints:
    """从 query 中解析 limit 和 order_by。"""

    def test_latest_one(self) -> None:
        limit, order = _parse_query_constraints("查询最新的一个PO")
        assert limit == 1
        assert order == "date_desc"

    def test_latest_n(self) -> None:
        limit, order = _parse_query_constraints("查最新的3条发票")
        assert limit == 3
        assert order == "date_desc"

    def test_recent_5(self) -> None:
        limit, order = _parse_query_constraints("最近5笔付款")
        assert limit == 5
        assert order == "date_desc"

    def test_first_n(self) -> None:
        limit, order = _parse_query_constraints("前10个采购订单")
        assert limit == 10
        assert order == "date_desc"

    def test_earliest_one(self) -> None:
        limit, order = _parse_query_constraints("最早的一个PO")
        assert limit == 1
        assert order == "date_asc"

    def test_amount_largest(self) -> None:
        limit, order = _parse_query_constraints("金额最大的3笔订单")
        assert limit == 3
        assert order == "amount_desc"

    def test_no_constraint(self) -> None:
        limit, order = _parse_query_constraints("查所有采购订单")
        assert limit == 0
        assert order == ""

    def test_resolve_uses_constraints_when_entity_present(self) -> None:
        """有实体编号时，query 中解析出的 limit/order_by 也会透传。"""
        result = resolve_lookup_tool(
            {"po_number": "PO-001"}, "查询 PO-001 最新的一个记录",
        )
        assert result is not None
        _, kwargs = result
        assert kwargs.get("limit") == 1
        assert kwargs.get("order_by") == "date_desc"
        assert kwargs.get("days") == 0  # 有实体编号时不限时间


# ── format_lookup_result 测试 ─────────────────────────────────────────


class TestFormatLookupResult:
    """format_lookup_result 格式化测试。"""

    def test_po_table(self) -> None:
        """PO 数据格式化为 Markdown 表格。"""
        data = [
            {
                "po_number": "PO-001",
                "vendor_name": "供应商A",
                "total_amount": 10000,
                "status": "approved",
                "order_date": "2024-01-15",
            }
        ]
        result = format_lookup_result(json.dumps(data), "query_purchase_orders")
        assert "PO-001" in result
        assert "供应商A" in result
        assert "| PO 编号" in result
        assert "共 1 条采购订单" in result

    def test_empty_list(self) -> None:
        """空列表返回"未找到"提示。"""
        result = format_lookup_result("[]", "query_purchase_orders")
        assert "未找到" in result

    def test_truncated_data(self) -> None:
        """带截断标记的数据正确展示总数。"""
        data = [
            {"po_number": "PO-001", "vendor_name": "A", "total_amount": 100,
             "status": "ok", "order_date": "2024-01-01"},
            {"_truncated": True, "dropped": 5,
             "reason": "系统预算裁剪：共 6 条，仅展示前 1 条"},
        ]
        result = format_lookup_result(json.dumps(data), "query_purchase_orders")
        assert "共 6 条" in result
        assert "展示前 1 条" in result

    def test_invalid_json(self) -> None:
        """非法 JSON 返回原始文本。"""
        result = format_lookup_result("not json", "query_purchase_orders")
        assert "not json" in result

    def test_invoice_columns(self) -> None:
        """发票数据使用正确的列定义。"""
        data = [
            {
                "invoice_num": "INV-001",
                "po_number": "PO-001",
                "total_amount": 5000,
                "status": "pending",
                "invoice_date": "2024-02-01",
            }
        ]
        result = format_lookup_result(json.dumps(data), "query_invoices")
        assert "| 发票号" in result
        assert "INV-001" in result

    def test_summary_with_limit_and_order(self) -> None:
        """带 limit + order_by 时摘要显示排序描述。"""
        data = [
            {"check_number": "PAY-001", "invoice_num": "INV-001",
             "amount": 5000, "status": "paid", "check_date": "2024-03-15"},
        ]
        result = format_lookup_result(
            json.dumps(data), "query_payments",
            limit=1, order_by="date_desc",
        )
        assert "最新的 1 条付款记录" in result

    def test_summary_with_limit_amount_desc(self) -> None:
        """带 limit + amount_desc 时摘要显示金额排序描述。"""
        data = [
            {"check_number": "PAY-001", "invoice_num": "INV-001",
             "amount": 9999, "status": "paid", "check_date": "2024-03-15"},
        ]
        result = format_lookup_result(
            json.dumps(data), "query_payments",
            limit=1, order_by="amount_desc",
        )
        assert "金额最大的 1 条付款记录" in result

    def test_summary_with_limit_only(self) -> None:
        """只有 limit 无 order_by 时显示简单计数。"""
        data = [
            {"check_number": "PAY-001", "invoice_num": "INV-001",
             "amount": 100, "status": "paid", "check_date": "2024-01-01"},
        ]
        result = format_lookup_result(
            json.dumps(data), "query_payments",
            limit=1,
        )
        assert "共 1 条付款记录" in result

    def test_unknown_tool(self) -> None:
        """未知工具名返回 JSON 原文。"""
        result = format_lookup_result('[{"a": 1}]', "unknown_tool")
        assert "1 条记录" in result


# ── execute_lookup 测试 ───────────────────────────────────────────────


class TestExecuteLookup:
    """execute_lookup 工具调用测试。"""

    @pytest.mark.asyncio
    async def test_normal_tool_call(self) -> None:
        """正常工具调用返回格式化结果。"""
        mock_tool = AsyncMock()
        mock_tool.ainvoke.return_value = json.dumps([
            {"po_number": "PO-001", "vendor_name": "A",
             "total_amount": 100, "status": "ok", "order_date": "2024-01-01"},
        ])

        registry = MagicMock()
        registry.get.return_value = mock_tool

        result = await execute_lookup(
            "query_purchase_orders",
            {"days": 30},
            registry,
        )
        assert result is not None
        assert "PO-001" in result
        mock_tool.ainvoke.assert_called_once_with({"days": 30})

    @pytest.mark.asyncio
    async def test_tool_not_registered(self) -> None:
        """工具未注册返回 None。"""
        registry = MagicMock()
        registry.get.return_value = None

        result = await execute_lookup("query_purchase_orders", {}, registry)
        assert result is None

    @pytest.mark.asyncio
    async def test_tool_raises_exception(self) -> None:
        """工具调用异常返回 None。"""
        mock_tool = AsyncMock()
        mock_tool.ainvoke.side_effect = RuntimeError("DB error")

        registry = MagicMock()
        registry.get.return_value = mock_tool

        result = await execute_lookup("query_purchase_orders", {}, registry)
        assert result is None

    @pytest.mark.asyncio
    async def test_supplier_combo(self) -> None:
        """supplier_combo 双工具调用。"""
        vendor_tool = AsyncMock()
        vendor_tool.ainvoke.return_value = json.dumps([
            {"vendor_id": "SUP-001", "vendor_name": "供应商A",
             "site": "上海", "terms_id": "NET30", "enabled_flag": "Y"},
        ])

        po_tool = AsyncMock()
        po_tool.ainvoke.return_value = json.dumps([
            {"po_number": "PO-001", "vendor_name": "供应商A",
             "total_amount": 100, "status": "ok", "order_date": "2024-01-01"},
        ])

        def mock_get(name: str):
            if name == "query_vendor_master":
                return vendor_tool
            if name == "query_purchase_orders":
                return po_tool
            return None

        registry = MagicMock()
        registry.get.side_effect = mock_get

        result = await execute_lookup(
            "__supplier_combo__",
            {"vendor_id": "SUP-001", "days": 30},
            registry,
        )
        assert result is not None
        assert "SUP-001" in result
        assert "PO-001" in result
