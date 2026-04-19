"""DATA_LOOKUP 快捷路径单元测试。

覆盖场景：
- resolve_lookup_tool：路径 A（实体编号）/ 路径 B（关键词推断）/ 未命中
- format_lookup_result：各实体类型的 Markdown 格式化
- execute_lookup：工具调用成功 / 工具未注册 / 调用异常
- 配置开关 lookup_shortcut_enabled 关闭时的行为
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
            {"invoice_number": "INV-001", "days": 7}, "查看发票INV-001"
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_invoices"
        assert kwargs["invoice_number"] == "INV-001"
        assert kwargs["days"] == 0  # 有实体编号时不限时间

    def test_path_a_payment_number(self) -> None:
        """路径 A：有 payment_number → query_payments。"""
        result = resolve_lookup_tool(
            {"payment_number": "PAY-001", "days": 30}, "查看付款单PAY-001"
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_payments"
        assert kwargs["payment_number"] == "PAY-001"

    def test_path_a_supplier_id(self) -> None:
        """路径 A：仅有 supplier_id → __supplier_combo__。"""
        result = resolve_lookup_tool(
            {"supplier_id": "SUP-001", "days": 30}, "查看供应商SUP-001"
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "__supplier_combo__"
        assert kwargs["supplier_id"] == "SUP-001"

    def test_path_a_priority_po_over_supplier(self) -> None:
        """路径 A：同时有 po_number 和 supplier_id 时优先 po_number。"""
        result = resolve_lookup_tool(
            {"po_number": "PO-001", "supplier_id": "SUP-001", "days": 30},
            "查看PO-001",
        )
        assert result is not None
        tool_name, _ = result
        assert tool_name == "query_purchase_orders"

    def test_path_b_keyword_po(self) -> None:
        """路径 B：关键词推断 → query_purchase_orders。"""
        result = resolve_lookup_tool({"days": 30}, "查最新的采购订单")
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_purchase_orders"
        assert kwargs["days"] == 30

    def test_path_b_keyword_invoice(self) -> None:
        """路径 B：关键词推断 → query_invoices。"""
        result = resolve_lookup_tool({"days": 30}, "列出最近的发票")
        assert result is not None
        tool_name, _ = result
        assert tool_name == "query_invoices"

    def test_path_b_keyword_payment(self) -> None:
        """路径 B：关键词推断 → query_payments。"""
        result = resolve_lookup_tool({"days": 30}, "看看最近的付款记录")
        assert result is not None
        tool_name, _ = result
        assert tool_name == "query_payments"

    def test_path_b_keyword_receipt(self) -> None:
        """路径 B：关键词推断 → query_receipts。"""
        result = resolve_lookup_tool({"days": 30}, "查最近的收货单")
        assert result is not None
        tool_name, _ = result
        assert tool_name == "query_receipts"

    def test_path_b_keyword_supplier(self) -> None:
        """路径 B：关键词推断 → query_vendor_master。"""
        result = resolve_lookup_tool({"days": 30}, "查看供应商列表")
        assert result is not None
        tool_name, _ = result
        assert tool_name == "query_vendor_master"

    def test_limit_and_order_by_passthrough_path_a(self) -> None:
        """路径 A：limit/order_by 透传到 kwargs。"""
        result = resolve_lookup_tool(
            {"po_number": "PO-001", "days": 30, "limit": 1, "order_by": "date_desc"},
            "查看最新的一个PO",
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_purchase_orders"
        assert kwargs["limit"] == 1
        assert kwargs["order_by"] == "date_desc"

    def test_limit_and_order_by_passthrough_path_b(self) -> None:
        """路径 B：limit/order_by 透传到 kwargs。"""
        result = resolve_lookup_tool(
            {"days": 30, "limit": 5, "order_by": "amount_desc"},
            "金额最大的5笔付款",
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_payments"
        assert kwargs["limit"] == 5
        assert kwargs["order_by"] == "amount_desc"

    def test_no_limit_order_by_when_absent(self) -> None:
        """不传 limit/order_by 时 kwargs 中不出现这些键。"""
        result = resolve_lookup_tool({"days": 30}, "查最新的采购订单")
        assert result is not None
        _, kwargs = result
        assert "limit" not in kwargs
        assert "order_by" not in kwargs

    def test_no_match_returns_none(self) -> None:
        """无实体无关键词 → None（降级 ReAct）。"""
        result = resolve_lookup_tool({"days": 30}, "帮我查一下")
        assert result is None

    def test_receipt_number_not_supported(self) -> None:
        """receipt_number 不在映射表中（工具缺参数），走路径 B 或兜底。"""
        # receipt_number 不被路径 A 识别，但 query 中有"收货"可被路径 B 命中
        result = resolve_lookup_tool(
            {"receipt_number": "RCV-001", "days": 30}, "查看收货单RCV-001"
        )
        assert result is not None
        tool_name, _ = result
        # 路径 A 不识别 receipt_number，路径 B 命中"收货单"关键词
        assert tool_name == "query_receipts"


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

    def test_resolve_uses_constraints(self) -> None:
        """resolve_lookup_tool should use parsed constraints."""
        tool_name, kwargs = resolve_lookup_tool({}, "查询最新的一个PO")  # type: ignore
        assert tool_name == "query_purchase_orders"
        assert kwargs.get("limit") == 1
        assert kwargs.get("order_by") == "date_desc"
        assert kwargs.get("days") == 365  # relaxed from 30

    def test_resolve_relaxes_days_with_limit(self) -> None:
        """When limit is set, days should be relaxed to 365."""
        _, kwargs = resolve_lookup_tool({"days": 30}, "最新的一条发票")  # type: ignore
        assert kwargs["days"] == 365


# ── format_lookup_result 测试 ─────────────────────────────────────────


class TestFormatLookupResult:
    """format_lookup_result 格式化测试。"""

    def test_po_table(self) -> None:
        """PO 数据格式化为 Markdown 表格。"""
        data = [
            {
                "po_number": "PO-001",
                "supplier_name": "供应商A",
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
            {"po_number": "PO-001", "supplier_name": "A", "total_amount": 100,
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
                "invoice_number": "INV-001",
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
            {"payment_number": "PAY-001", "invoice_number": "INV-001",
             "amount": 5000, "status": "paid", "payment_date": "2024-03-15"},
        ]
        result = format_lookup_result(
            json.dumps(data), "query_payments",
            limit=1, order_by="date_desc",
        )
        assert "最新的 1 条付款记录" in result

    def test_summary_with_limit_amount_desc(self) -> None:
        """带 limit + amount_desc 时摘要显示金额排序描述。"""
        data = [
            {"payment_number": "PAY-001", "invoice_number": "INV-001",
             "amount": 9999, "status": "paid", "payment_date": "2024-03-15"},
        ]
        result = format_lookup_result(
            json.dumps(data), "query_payments",
            limit=1, order_by="amount_desc",
        )
        assert "金额最大的 1 条付款记录" in result

    def test_summary_with_limit_only(self) -> None:
        """只有 limit 无 order_by 时显示简单计数。"""
        data = [
            {"payment_number": "PAY-001", "invoice_number": "INV-001",
             "amount": 100, "status": "paid", "payment_date": "2024-01-01"},
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
            {"po_number": "PO-001", "supplier_name": "A",
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
            {"supplier_id": "SUP-001", "supplier_name": "供应商A",
             "site": "上海", "payment_terms": "NET30", "status": "active"},
        ])

        po_tool = AsyncMock()
        po_tool.ainvoke.return_value = json.dumps([
            {"po_number": "PO-001", "supplier_name": "供应商A",
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
            {"supplier_id": "SUP-001", "days": 30},
            registry,
        )
        assert result is not None
        assert "SUP-001" in result
        assert "PO-001" in result
