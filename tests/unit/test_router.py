"""IntentRouter 三级路由单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from api.schemas.analysis import AnalysisType
from config.settings import Settings
from core.orchestrator.router import IntentRouter, _extract_params, _RULE_LIBRARY
from core.orchestrator.signal import QuerySignal


# ============================================================
# _extract_params 参数提取测试
# ============================================================


class TestExtractParams:
    """正则参数提取（复用原 IntentParser 逻辑）。"""

    def test_extract_supplier_id(self) -> None:
        params = _extract_params("查看 SUP-001 的采购订单")
        assert params["supplier_id"] == "SUP-001"

    def test_extract_po_number(self) -> None:
        params = _extract_params("检查 PO-2024-0001 的匹配情况")
        assert "po_number" in params

    def test_extract_days_chinese(self) -> None:
        params = _extract_params("分析最近60天的数据")
        assert params["days"] == 60

    def test_extract_days_english(self) -> None:
        params = _extract_params("analyze past 90 days")
        assert params["days"] == 90

    def test_no_params(self) -> None:
        params = _extract_params("给我一份分析报告")
        assert params == {}


# ============================================================
# Level 1：关键词命中率测试
# ============================================================


class TestLevel1:
    """关键词命中率评分路由。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def test_three_way_match(self) -> None:
        signal = self.router._try_level1("分析Q1三路匹配异常", {})
        assert signal is not None
        assert signal.keywords == [AnalysisType.THREE_WAY_MATCH.value]
        assert signal.route_level == 1
        assert signal.confidence > 0

    def test_price_variance(self) -> None:
        signal = self.router._try_level1("检查价格差异情况", {})
        assert signal is not None
        assert signal.keywords == [AnalysisType.PRICE_VARIANCE.value]

    def test_payment_compliance(self) -> None:
        signal = self.router._try_level1("分析付款逾期风险", {})
        assert signal is not None
        assert signal.keywords == [AnalysisType.PAYMENT_COMPLIANCE.value]

    def test_supplier_performance(self) -> None:
        signal = self.router._try_level1("评估供应商绩效 KPI", {})
        assert signal is not None
        assert signal.keywords == [AnalysisType.SUPPLIER_PERFORMANCE.value]

    def test_no_match(self) -> None:
        """无关键词命中应返回 None。"""
        signal = self.router._try_level1("今天天气怎么样", {})
        assert signal is None

    def test_params_passed_through(self) -> None:
        """提取的参数应传递到 signal.entities。"""
        params = {"supplier_id": "SUP-001", "days": 30}
        signal = self.router._try_level1("分析三路匹配异常", params)
        assert signal is not None
        assert signal.entities["supplier_id"] == "SUP-001"
        assert signal.time_range_days == 30

    def test_hit_rate_scoring(self) -> None:
        """多个关键词命中应提升置信度。"""
        # "三路匹配发票收货异常" 命中多个关键词 → 高置信度
        signal_more = self.router._try_level1("三路匹配发票收货异常", {})
        # "三路匹配" 命中较少关键词 → 低置信度
        signal_less = self.router._try_level1("分析三路匹配", {})
        assert signal_more is not None
        assert signal_less is not None
        assert signal_more.confidence > signal_less.confidence

    def test_single_vague_keyword_no_match(self) -> None:
        """单个模糊关键词（如'匹配'）命中率过低不应触发 Level 1。"""
        signal = self.router._try_level1("匹配", {})
        assert signal is None


# ============================================================
# Level 2：Chroma 种子库语义匹配测试
# ============================================================


class TestLevel2:
    """Chroma 种子库语义匹配（mock VectorStore）。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def test_high_similarity_match(self) -> None:
        """高相似度应命中 Level 2。"""
        mock_store = MagicMock()
        mock_store.search.return_value = [
            {
                "id": "seed_price_variance_0",
                "text": "实际价格超出合同价",
                "metadata": {"analysis_type": "price_variance"},
                "distance": 0.1,  # similarity = 0.9
            }
        ]
        self.router._seeds_store = mock_store
        self.router._seeds_loaded = True

        signal = self.router._try_level2("为什么采购成本比预算高", {})
        assert signal is not None
        assert signal.route_level == 2
        assert signal.keywords == [AnalysisType.PRICE_VARIANCE.value]
        assert signal.confidence >= 0.80

    def test_low_similarity_skip(self) -> None:
        """低相似度应跳过 Level 2。"""
        mock_store = MagicMock()
        mock_store.search.return_value = [
            {
                "id": "seed_0",
                "text": "无关内容",
                "metadata": {"analysis_type": "three_way_match"},
                "distance": 0.5,  # similarity = 0.5 < 0.80
            }
        ]
        self.router._seeds_store = mock_store
        self.router._seeds_loaded = True

        signal = self.router._try_level2("完全无关的查询", {})
        assert signal is None

    def test_empty_results_skip(self) -> None:
        """空检索结果应跳过。"""
        mock_store = MagicMock()
        mock_store.search.return_value = []
        self.router._seeds_store = mock_store
        self.router._seeds_loaded = True

        signal = self.router._try_level2("查询", {})
        assert signal is None

    def test_store_init_failure_skip(self) -> None:
        """VectorStore 初始化失败应跳过 Level 2（不阻断）。"""
        with patch.object(
            self.router, "_ensure_seeds_store", side_effect=RuntimeError("chroma down")
        ):
            signal = self.router._try_level2("查询", {})
        assert signal is None

    def test_search_failure_skip(self) -> None:
        """搜索失败应跳过 Level 2。"""
        mock_store = MagicMock()
        mock_store.search.side_effect = RuntimeError("search error")
        self.router._seeds_store = mock_store
        self.router._seeds_loaded = True

        signal = self.router._try_level2("查询", {})
        assert signal is None


# ============================================================
# Level 3：LLM 分类测试
# ============================================================


class TestLevel3:
    """LLM 分类兜底（mock LLM）。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def test_llm_classify_success(self) -> None:
        """LLM 成功分类。"""
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"type": "price_variance", "confidence": 0.85, "supplier_id": null, "po_number": null, "days": 30}'
        mock_llm.invoke.return_value = mock_response
        self.router._llm = mock_llm

        signal = self.router._try_level3("为什么采购成本涨了", {})
        assert signal.route_level == 3
        assert signal.keywords == [AnalysisType.PRICE_VARIANCE.value]
        assert signal.confidence == 0.85
        assert signal.entities.get("days") == 30

    def test_llm_classify_with_code_block(self) -> None:
        """LLM 输出包裹 markdown 代码块时应正确解析。"""
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '```json\n{"type": "supplier_performance", "confidence": 0.9, "supplier_id": "SUP-001", "po_number": null, "days": null}\n```'
        mock_llm.invoke.return_value = mock_response
        self.router._llm = mock_llm

        signal = self.router._try_level3("供应商交货怎么样", {})
        assert signal.keywords == [AnalysisType.SUPPLIER_PERFORMANCE.value]
        assert signal.entities.get("supplier_id") == "SUP-001"

    def test_llm_failure_fallback(self) -> None:
        """LLM 调用失败应降级为 COMPREHENSIVE。"""
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = RuntimeError("API timeout")
        self.router._llm = mock_llm

        signal = self.router._try_level3("任意查询", {})
        assert signal.route_level == 3
        assert signal.keywords == [AnalysisType.COMPREHENSIVE.value]
        assert signal.confidence == 0.0

    def test_regex_params_override_llm(self) -> None:
        """正则提取的参数应覆盖 LLM 提取的。"""
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"type": "three_way_match", "confidence": 0.8, "supplier_id": "SUP-999", "po_number": null, "days": 60}'
        mock_llm.invoke.return_value = mock_response
        self.router._llm = mock_llm

        regex_params = {"supplier_id": "SUP-001", "days": 30}
        signal = self.router._try_level3("查询", regex_params)
        # 正则提取的 SUP-001 和 30 天应覆盖 LLM 的 SUP-999 和 60
        assert signal.entities["supplier_id"] == "SUP-001"
        assert signal.entities["days"] == 30

    def test_llm_invalid_type_fallback(self) -> None:
        """LLM 返回无效 type 时应降级为 COMPREHENSIVE。"""
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"type": "invalid_type", "confidence": 0.5}'
        mock_llm.invoke.return_value = mock_response
        self.router._llm = mock_llm

        signal = self.router._try_level3("查询", {})
        assert signal.keywords == [AnalysisType.COMPREHENSIVE.value]


# ============================================================
# parse() 集成测试（端到端三级路由）
# ============================================================


class TestParseIntegration:
    """parse() 方法端到端测试。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def test_level1_hit(self) -> None:
        """Level 1 命中时应直接返回，不走 Level 2/3。"""
        analysis_type, params = self.router.parse("分析Q1三路匹配异常")
        assert analysis_type == AnalysisType.THREE_WAY_MATCH

    def test_level1_with_params(self) -> None:
        """Level 1 命中时参数应正确提取。"""
        analysis_type, params = self.router.parse("分析 SUP-001 最近60天的价格差异")
        assert analysis_type == AnalysisType.PRICE_VARIANCE
        assert params.get("supplier_id") == "SUP-001"
        assert params.get("days") == 60

    def test_level3_fallback_on_no_match(self) -> None:
        """Level 1/2 均未命中时应走 Level 3（mock LLM）。"""
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"type": "comprehensive", "confidence": 0.5}'
        mock_llm.invoke.return_value = mock_response
        self.router._llm = mock_llm

        # 跳过 Level 2（mock store 返回空结果）
        mock_store = MagicMock()
        mock_store.search.return_value = []
        self.router._seeds_store = mock_store
        self.router._seeds_loaded = True

        analysis_type, _ = self.router.parse("哪些采购员的谈判能力弱")
        assert analysis_type == AnalysisType.COMPREHENSIVE

    def test_backward_compatible_with_old_tests(self) -> None:
        """与原 IntentParser 的测试用例保持兼容。"""
        # 这些用例来自 test_intent.py，确保行为一致
        t, _ = self.router.parse("分析最近三个月的三路匹配异常")
        assert t == AnalysisType.THREE_WAY_MATCH

        t, _ = self.router.parse("检查价格差异情况")
        assert t == AnalysisType.PRICE_VARIANCE

        t, _ = self.router.parse("分析付款逾期风险")
        assert t == AnalysisType.PAYMENT_COMPLIANCE

        t, _ = self.router.parse("评估供应商绩效")
        assert t == AnalysisType.SUPPLIER_PERFORMANCE

        _, params = self.router.parse("查看 SUP-001 的采购订单")
        assert params.get("supplier_id") == "SUP-001"

        _, params = self.router.parse("分析最近60天的三路匹配异常")
        assert params.get("days") == 60


# ============================================================
# QuerySignal 数据结构测试
# ============================================================


class TestQuerySignal:
    """QuerySignal dataclass 基本测试。"""

    def test_default_values(self) -> None:
        signal = QuerySignal(raw_query="test")
        assert signal.keywords == []
        assert signal.entities == {}
        assert signal.time_range_days is None
        assert signal.route_level == 0
        assert signal.confidence == 0.0
        assert signal.reasoning == ""

    def test_full_construction(self) -> None:
        signal = QuerySignal(
            raw_query="分析价格差异",
            keywords=["price_variance"],
            entities={"supplier_id": "SUP-001"},
            time_range_days=30,
            route_level=1,
            confidence=0.85,
            reasoning="L1 命中",
        )
        assert signal.raw_query == "分析价格差异"
        assert signal.time_range_days == 30
        assert signal.confidence == 0.85


# ============================================================
# 种子库加载测试
# ============================================================


class TestSeedsLoading:
    """intent_seeds.yaml 加载逻辑。"""

    def test_load_seeds_called_on_first_access(self) -> None:
        """首次访问 seeds store 时应加载种子数据。"""
        router = IntentRouter(settings=Settings())

        mock_instance = MagicMock()

        with patch(
            "core.knowledge.vector_store.VectorStore.from_settings",
            return_value=mock_instance,
        ):
            with patch.object(router, "_load_seeds") as mock_load:
                router._ensure_seeds_store()
                mock_load.assert_called_once()

    def test_seeds_not_reloaded(self) -> None:
        """种子数据只加载一次。"""
        router = IntentRouter(settings=Settings())
        router._seeds_loaded = True

        mock_store = MagicMock()

        with patch(
            "core.knowledge.vector_store.VectorStore.from_settings",
            return_value=mock_store,
        ):
            with patch.object(router, "_load_seeds") as mock_load:
                router._ensure_seeds_store()
                mock_load.assert_not_called()
