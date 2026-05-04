"""DATA_LOOKUP 快捷路径单元测试。

覆盖场景：
- resolve_lookup_tool 路径 A：实体编号精确查询
- resolve_lookup_tool 路径 B：四重漏斗高置信度关键词映射
  · 漏斗 #1 实体类型唯一（单类命中 / 多类或零类 miss）
  · 漏斗 #2 有数量/排序修饰 或 明确时间窗
  · 漏斗 #3 无分析/诊断/概览黑名单词
  · 漏斗 #4 无关系追溯词
- format_lookup_result：各实体类型的 Markdown 格式化
- execute_lookup：工具调用成功 / 工具未注册 / 调用异常

历史 bad case 回归：
  "查询最近 7 天的采购订单概况" 等含"概况/异常"等词的查询必须 miss（漏斗 #3）。
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
from modules.p2p.provider import P2PModuleProvider

_p2p_provider = P2PModuleProvider()


# ── resolve_lookup_tool 测试 ──────────────────────────────────────────


class TestResolveLookupTool:
    """resolve_lookup_tool 路由逻辑测试。"""

    def test_path_a_po_number(self) -> None:
        """路径 A：有 po_number → query_purchase_orders，days=0（不限时间）。"""
        result = resolve_lookup_tool(
            {"po_number": "PO-2024-001", "days": 30}, "查看PO-2024-001",
            provider=_p2p_provider,
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_purchase_orders"
        assert kwargs["po_number"] == "PO-2024-001"
        assert kwargs["days"] == 0  # 有实体编号时不限时间

    def test_path_a_invoice_number(self) -> None:
        """路径 A：有 invoice_number → query_invoices，days=0（不限时间）。"""
        result = resolve_lookup_tool(
            {"invoice_num": "INV-001", "days": 7}, "查看发票INV-001",
            provider=_p2p_provider,
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_invoices"
        assert kwargs["invoice_num"] == "INV-001"
        assert kwargs["days"] == 0  # 有实体编号时不限时间

    def test_path_a_payment_number(self) -> None:
        """路径 A：有 payment_number → query_payments。"""
        result = resolve_lookup_tool(
            {"check_number": "PAY-001", "days": 30}, "查看付款单PAY-001",
            provider=_p2p_provider,
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_payments"
        assert kwargs["check_number"] == "PAY-001"

    def test_path_a_supplier_id(self) -> None:
        """路径 A：仅有 supplier_id → __supplier_combo__。"""
        result = resolve_lookup_tool(
            {"vendor_id": "SUP-001", "days": 30}, "查看供应商SUP-001",
            provider=_p2p_provider,
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
            provider=_p2p_provider,
        )
        assert result is not None
        tool_name, _ = result
        assert tool_name == "query_purchase_orders"

    def test_limit_and_order_by_passthrough_with_entity(self) -> None:
        """有实体编号时 limit/order_by 仍正确透传。"""
        result = resolve_lookup_tool(
            {"po_number": "PO-001", "days": 30, "limit": 1, "order_by": "date_desc"},
            "查看最新的一个PO",
            provider=_p2p_provider,
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_purchase_orders"
        assert kwargs["limit"] == 1
        assert kwargs["order_by"] == "date_desc"

    def test_no_match_returns_none(self) -> None:
        """无实体无法命中 → None（由 orchestrator 进入 PS 或 ReAct）。"""
        result = resolve_lookup_tool({"days": 30}, "帮我查一下", provider=_p2p_provider)
        assert result is None

    def test_receipt_number_falls_into_path_b(self) -> None:
        """receipt_number 未纳入路径 A 映射表；但查询含"收货单"+时间窗会被路径 B 命中。"""
        result = resolve_lookup_tool(
            {"receipt_number": "RCV-001", "days": 7},
            "查看最近 7 天的收货单",
            provider=_p2p_provider,
        )
        assert result is not None
        tool_name, _ = result
        assert tool_name == "query_receipts"


# ── 路径 B 四重漏斗测试 ─────────────────────────────────────────────


class TestPathBFourFilters:
    """路径 B：四重漏斗逻辑测试。"""

    # ─ 命中场景 ─

    def test_hit_latest_n_po(self) -> None:
        """漏斗全满足：'最新 5 个 PO' → query_purchase_orders。"""
        result = resolve_lookup_tool({"days": 30}, "最新的 5 个 PO", provider=_p2p_provider)
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_purchase_orders"
        assert kwargs["limit"] == 5
        assert kwargs["order_by"] == "date_desc"

    def test_hit_largest_amount_payments(self) -> None:
        """漏斗全满足：'金额最大的 3 笔付款' → query_payments。"""
        result = resolve_lookup_tool({"days": 30}, "金额最大的 3 笔付款", provider=_p2p_provider)
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_payments"
        assert kwargs["order_by"] == "amount_desc"

    def test_hit_with_explicit_time_window(self) -> None:
        """漏斗 #2 放宽：明确时间窗（"最近 7 天"）+ 无数量修饰也命中。"""
        result = resolve_lookup_tool(
            {"days": 7}, "查询最近 7 天的发票",
            provider=_p2p_provider,
        )
        assert result is not None
        tool_name, _ = result
        assert tool_name == "query_invoices"

    def test_hit_supplier_list(self) -> None:
        """'本月的供应商列表' → 时间窗命中漏斗 #2。"""
        result = resolve_lookup_tool({}, "本月的供应商列表", provider=_p2p_provider)
        assert result is not None
        tool_name, _ = result
        assert tool_name == "query_vendor_master"

    # ─ 漏斗 #1 miss：实体类型多类或零类 ─

    def test_miss_multi_entity_categories(self) -> None:
        """漏斗 #1 miss：同时含 PO + 发票 → 多类别放行。"""
        result = resolve_lookup_tool(
            {"days": 7}, "列出最近 7 天的采购订单和发票",
            provider=_p2p_provider,
        )
        assert result is None

    def test_miss_no_entity_category(self) -> None:
        """漏斗 #1 miss：query 中无任何实体类别词。"""
        result = resolve_lookup_tool({"days": 7}, "最近 7 天有什么值得关注的", provider=_p2p_provider)
        assert result is None

    # ─ 漏斗 #2 miss：无数量/排序 + 无时间窗 ─

    def test_miss_no_quantity_no_time_window(self) -> None:
        """漏斗 #2 miss：只有实体词，无数量/排序/时间窗。"""
        result = resolve_lookup_tool({"days": 30}, "查一下发票", provider=_p2p_provider)
        assert result is None

    # ─ 漏斗 #3 miss：黑名单词（回归 bad case）─

    def test_miss_bad_case_procurement_overview(self) -> None:
        """历史 bad case 回归：'查询最近 7 天的采购订单概况' 必须 miss。"""
        assert resolve_lookup_tool(
            {"days": 7}, "查询最近 7 天的采购订单概况",
            provider=_p2p_provider,
        ) is None

    def test_miss_blacklist_analysis_words(self) -> None:
        """漏斗 #3 miss：含"异常/对比/分析/为什么/风险"等词。"""
        for q in (
            "最近 7 天的采购订单异常",
            "对比最新 5 个 PO 的差异",
            "分析最近一周的付款",
            "为什么最新 3 张发票金额偏高",
            "最近采购订单风险如何",
            "最近付款合规情况",
            "评估最新发票",
        ):
            assert resolve_lookup_tool({"days": 7}, q, provider=_p2p_provider) is None, q

    # ─ 漏斗 #4 miss：关系追溯词 ─

    def test_miss_graph_intent_words(self) -> None:
        """漏斗 #4 miss：含"链路/关联/追踪/上下游/溯源"等词。"""
        for q in (
            "追踪最新 PO 的完整链路",
            "查看最近 5 个 PO 的上下游关系",
            "溯源最近的付款路径",
        ):
            assert resolve_lookup_tool({"days": 7}, q, provider=_p2p_provider) is None, q

    # ─ 路径 A 和路径 B 优先级 ─

    def test_path_a_precedes_path_b(self) -> None:
        """有实体编号时走路径 A，不经过漏斗。"""
        result = resolve_lookup_tool(
            {"po_number": "PO-001"}, "PO-001 的异常情况",  # "异常/情况" 本是黑名单词
            provider=_p2p_provider,
        )
        assert result is not None
        tool_name, kwargs = result
        assert tool_name == "query_purchase_orders"
        assert kwargs["po_number"] == "PO-001"


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
            provider=_p2p_provider,
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
    async def test_empty_result_returns_none(self) -> None:
        """工具返回空列表时 execute_lookup 应返回 None（触发 fallback）。"""
        mock_tool = AsyncMock()
        mock_tool.ainvoke.return_value = "[]"

        registry = MagicMock()
        registry.get.return_value = mock_tool

        result = await execute_lookup(
            "query_purchase_orders", {"days": 1}, registry,
        )
        assert result is None

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
