"""意图路由准确性测试。

bypass 检测走真实正则，参数提取走真实正则，LLM 分类走确定性映射。
"""

from __future__ import annotations

import pytest

from core.orchestrator.signal import IntentKind
from tests.fixtures.queries import get_queries_by_tag


class TestBypassRouting:
    """bypass 层（正则，完全真实）。"""

    @pytest.mark.parametrize(
        "query,expected_kind",
        [
            ("你好", "chitchat"),
            ("谢谢", "chitchat"),
            ("你能做什么", "meta"),
            ("支持哪些分析", "meta"),
            ("上次分析了什么", "recall"),
            ("刚才的结果", "recall"),
        ],
    )
    def test_bypass(self, deterministic_router, query, expected_kind):
        signal = deterministic_router.route(query)
        assert signal.intent_kind.value == expected_kind


class TestParamExtraction:
    """参数提取（正则，完全真实，不经过 LLM）。"""

    @pytest.mark.parametrize(
        "query,field,expected",
        [
            ("检查PO-2024-0001的匹配情况", "po_number", "PO-2024-0001"),
            ("查看发票INV-2024-0001", "invoice_num", "INV-2024-0001"),
            ("查询付款单PAY-001", "check_number", "PAY-001"),
            ("SUP-001的采购订单", "vendor_id", "SUP-001"),
        ],
    )
    def test_extract_entity(self, deterministic_router, query, field, expected):
        signal = deterministic_router.route(query)
        assert signal.entities.get(field) == expected

    def test_extract_days(self, deterministic_router):
        signal = deterministic_router.route("分析最近60天的数据")
        assert signal.entities.get("days") == 60


class TestIntentClassification:
    """意图分类（LLM 部分走确定性映射）。"""

    @pytest.mark.parametrize(
        "cq", get_queries_by_tag("lookup"), ids=lambda c: c.query,
    )
    def test_lookup_queries_get_data_lookup(self, deterministic_router, cq):
        signal = deterministic_router.route(cq.query)
        assert signal.intent_kind == IntentKind.DATA_LOOKUP

    @pytest.mark.parametrize(
        "cq", get_queries_by_tag("dag"), ids=lambda c: c.query,
    )
    def test_dag_queries_get_analysis(self, deterministic_router, cq):
        signal = deterministic_router.route(cq.query)
        assert signal.intent_kind == IntentKind.ANALYSIS

    @pytest.mark.parametrize(
        "cq", get_queries_by_tag("ps"), ids=lambda c: c.query,
    )
    def test_ps_queries_get_analysis(self, deterministic_router, cq):
        signal = deterministic_router.route(cq.query)
        assert signal.intent_kind == IntentKind.ANALYSIS

    def test_blacklist_blocks_lookup(self, deterministic_router):
        signal = deterministic_router.route("为什么最新的PO金额这么高")
        assert signal.intent_kind == IntentKind.ANALYSIS

    def test_cross_entity_gets_analysis(self, deterministic_router):
        signal = deterministic_router.route("列出最近7天的采购订单和发票")
        assert signal.intent_kind == IntentKind.ANALYSIS
        assert signal.is_cross_entity is True
