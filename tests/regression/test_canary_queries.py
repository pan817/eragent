"""金丝雀查询回归基线。

每条查询验证三个层面：
1. 路由结果匹配预期
2. lookup 路径命中正确工具
3. 返回数据满足最低约束（行数、字段、排序）
"""

from __future__ import annotations

import json

import pytest

from core.orchestrator.lookup import resolve_lookup_tool
from modules.p2p.provider import P2PModuleProvider
from tests.fixtures.queries import CANARY_QUERIES, CanaryQuery, get_queries_by_tag

_provider = P2PModuleProvider()


class TestCanaryRouteMatches:
    """验证路由结果与预期一致。"""

    @pytest.mark.parametrize("cq", CANARY_QUERIES, ids=lambda c: c.query)
    def test_route_intent(self, cq: CanaryQuery, deterministic_router):
        signal = deterministic_router.route(cq.query)
        assert signal.intent_kind.value == cq.expected_intent.lower()


class TestCanaryToolHit:
    """验证 lookup 路径命中正确工具。"""

    @pytest.mark.parametrize(
        "cq",
        get_queries_by_tag("lookup"),
        ids=lambda c: c.query,
    )
    def test_lookup_tool_resolve(self, cq: CanaryQuery):
        if cq.expected_route != "lookup_shortcut":
            pytest.skip("non-lookup route")

        if cq.expected_tool == "query_vendor_master":
            # vendor_master 走 supplier_combo 特殊路径
            result = resolve_lookup_tool(
                {"vendor_id": "SUP-001"} if "供应商" in cq.query else {"days": 30},
                cq.query,
                provider=_provider,
            )
        elif "entity" in cq.tags:
            params: dict = {"days": 30}
            if "PO-001" in cq.query:
                params["po_number"] = "PO-001"
            elif "SUP-001" in cq.query:
                params["vendor_id"] = "SUP-001"
            result = resolve_lookup_tool(params, cq.query, provider=_provider)
        else:
            result = resolve_lookup_tool({"days": 30}, cq.query, provider=_provider)

        assert result is not None, f"lookup miss for: {cq.query}"
        tool_name, _kwargs = result
        if tool_name == "__supplier_combo__":
            assert cq.expected_tool in ("query_purchase_orders", "query_vendor_master")
        else:
            assert tool_name == cq.expected_tool


class TestCanaryResultQuality:
    """验证返回数据满足最低质量约束。"""

    @pytest.mark.parametrize(
        "cq",
        [q for q in get_queries_by_tag("lookup") if q.min_result_count is not None],
        ids=lambda c: c.query,
    )
    async def test_result_quality(self, cq: CanaryQuery, tool_registry):
        if cq.expected_tool is None:
            pytest.skip("no tool expected")

        tool_fn = tool_registry.get(cq.expected_tool)
        if tool_fn is None:
            pytest.skip(f"tool {cq.expected_tool} not in registry")

        kwargs: dict = {"days": 0}
        if cq.assert_order_field == "creation_date" and cq.assert_order_dir == "desc":
            kwargs["order_by"] = "date_desc"
        elif cq.assert_order_field == "amount" and cq.assert_order_dir == "desc":
            kwargs["order_by"] = "amount_desc"
        if cq.min_result_count:
            kwargs["limit"] = cq.min_result_count

        raw = await tool_fn.ainvoke(kwargs)
        data = json.loads(raw)

        items = [d for d in data if not (isinstance(d, dict) and d.get("_truncated"))]

        assert len(items) >= cq.min_result_count, (
            f"expected >= {cq.min_result_count} results, got {len(items)}"
        )

        if cq.must_contain_fields:
            for field in cq.must_contain_fields:
                assert field in items[0], f"missing field: {field}"

        if cq.assert_order_field and len(items) >= 2:
            values = [item[cq.assert_order_field] for item in items]
            if cq.assert_order_dir == "desc":
                assert values == sorted(values, reverse=True), (
                    f"not sorted desc by {cq.assert_order_field}"
                )
            elif cq.assert_order_dir == "asc":
                assert values == sorted(values), (
                    f"not sorted asc by {cq.assert_order_field}"
                )
