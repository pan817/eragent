"""IntentRouter 三级路由单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from api.schemas.analysis import AnalysisType
from config.settings import Settings
from core.orchestrator.router import (
    IntentRouter,
    _classify_bypass,
    _extract_params,
    _is_non_analysis_query,
    _looks_like_data_lookup,
    _RULE_LIBRARY,
)
from core.orchestrator.signal import IntentKind, QuerySignal


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

    def test_extract_invoice_number(self) -> None:
        params = _extract_params("查看发票 INV-2024-0001 的详情")
        assert params["invoice_number"] == "INV-2024-0001"

    def test_extract_payment_number(self) -> None:
        params = _extract_params("查询付款单 PAY-2024-0500")
        assert params["payment_number"] == "PAY-2024-0500"

    def test_extract_receipt_number(self) -> None:
        params = _extract_params("收货单 RCV-2024-0100 有问题")
        assert params["receipt_number"] == "RCV-2024-0100"

    def test_extract_multiple_entities(self) -> None:
        params = _extract_params("PO-001 的发票 INV-001 付款 PAY-001 来自 SUP-001")
        assert params.get("po_number") == "PO-001"
        assert params.get("invoice_number") == "INV-001"
        assert params.get("payment_number") == "PAY-001"
        assert params.get("supplier_id") == "SUP-001"

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

    def test_llm_legacy_unknown_type_falls_back_to_clarification(self) -> None:
        """老版本 prompt 仅返回 type=unknown 时，应兼容映射为 CLARIFICATION 信号。"""
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = (
            '{"type": "unknown", "confidence": 0.7, '
            '"supplier_id": null, "po_number": null, "days": null}'
        )
        mock_llm.invoke.return_value = mock_response
        self.router._llm = mock_llm

        signal = self.router._try_level3("今天天气怎么样", {})
        assert signal.route_level == 3
        assert signal.intent_kind == IntentKind.CLARIFICATION
        assert signal.confidence == 0.7

    def test_llm_classify_prompt_contains_intent_kind_enum(self) -> None:
        """L3 prompt 应列出新版 intent_kind 枚举与各类下游处理说明。"""
        prompt = self.router._LLM_CLASSIFY_PROMPT
        # 6 类 intent_kind 全部出现
        for kind in [
            "analysis",
            "data_lookup",
            "clarification",
            "meta",
            "chitchat",
            "out_of_scope",
        ]:
            assert kind in prompt, f"intent_kind '{kind}' 未在 prompt 中出现"

    def test_llm_classify_prompt_contains_few_shot_examples(self) -> None:
        """L3 prompt 应包含边界 case 的 few-shot 示例。"""
        prompt = self.router._LLM_CLASSIFY_PROMPT
        assert "查询最新的一个 PO" in prompt  # data_lookup 示例
        assert "做一下三路匹配" in prompt  # clarification 示例
        assert "今天天气" in prompt  # chitchat 示例

    def test_llm_classify_prompt_contains_confidence_anchors(self) -> None:
        """L3 prompt 应包含 confidence 分档锚点（P1）。"""
        prompt = self.router._LLM_CLASSIFY_PROMPT
        assert "confidence 判定锚点" in prompt or "判定锚点" in prompt
        # 至少包含 3 个分档阈值
        assert ">0.9" in prompt
        assert "0.7-0.9" in prompt
        assert "<0.5" in prompt

    def test_llm_classify_prompt_injects_current_date(self) -> None:
        """L3 prompt 在 _try_level3 渲染后应包含当前日期与时区（P1）。"""
        import re
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"type": "comprehensive", "confidence": 0.5}'
        mock_llm.invoke.return_value = mock_response
        self.router._llm = mock_llm

        self.router._try_level3("测试", {})
        rendered_prompt = mock_llm.invoke.call_args[0][0]

        assert re.search(r"\d{4}-\d{2}-\d{2}", rendered_prompt)
        assert "Asia/Shanghai" in rendered_prompt
        assert "当前日期" in rendered_prompt

    def test_ensure_llm_passes_disable_thinking(self) -> None:
        """_ensure_llm 应传 disable_thinking=True 给 build_chat_model。

        L3 是单轮 JSON 分类，无需链式推理；关 thinking 可省 token、降延迟，
        并避免思考内容混入 content 导致 json.loads 失败。

        走小模型 llm_fast（未配置时在 from_yaml 里自动镜像 llm）。
        """
        settings = Settings()
        router = IntentRouter(settings=settings)
        with patch("core.llm.model_factory.build_chat_model") as mock_build:
            router._ensure_llm()
            mock_build.assert_called_once_with(settings.llm_fast, disable_thinking=True)


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


class TestTraceHelpers:
    """路由 trace 辅助方法测试。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def test_evaluate_all_rules(self) -> None:
        """_evaluate_all_rules 应返回所有规则的评分。"""
        scores = self.router._evaluate_all_rules("分析三路匹配发票异常")
        assert len(scores) == 10  # 10 条规则（4 原始 + 6 新增）
        assert all("rule" in s and "hit_rate" in s and "threshold" in s for s in scores)
        # 三路匹配规则应命中
        twm = next(s for s in scores if s["rule"] == "three_way_match")
        assert twm["matched"] is True
        assert len(twm["hit_keywords"]) > 0

    def test_search_seeds_for_trace_no_store(self) -> None:
        """_search_seeds_for_trace store 未初始化时返回空列表。"""
        results = self.router._search_seeds_for_trace("任意查询")
        # 可能初始化 Chroma 也可能失败，都应返回 list
        assert isinstance(results, list)


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


# ============================================================
# analyst_role L3 注入
# ============================================================


class TestAnalystRoleInjection:
    """analyst_role 角色注入到 L3 prompt 的测试。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def test_role_passed_to_l3_prompt(self) -> None:
        """analyst_role 应注入到 L3 LLM prompt 中。"""
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"type": "payment_compliance", "confidence": 0.9}'
        mock_llm.invoke.return_value = mock_response
        self.router._llm = mock_llm

        # L1/L2 都不命中，走 L3
        signal = self.router._try_level3(
            "最近有什么异常", {}, analyst_role="finance"
        )

        # 验证 prompt 中包含角色描述
        call_args = mock_llm.invoke.call_args[0][0]
        assert "财务合规人员" in call_args
        assert "付款逾期" in call_args

    def test_general_role_no_injection(self) -> None:
        """general 角色不注入角色描述。"""
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"type": "comprehensive", "confidence": 0.5}'
        mock_llm.invoke.return_value = mock_response
        self.router._llm = mock_llm

        signal = self.router._try_level3(
            "最近有什么异常", {}, analyst_role="general"
        )

        call_args = mock_llm.invoke.call_args[0][0]
        assert "用户角色" not in call_args

    def test_procurement_role_injection(self) -> None:
        """procurement 角色应注入采购相关描述。"""
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"type": "price_variance", "confidence": 0.85}'
        mock_llm.invoke.return_value = mock_response
        self.router._llm = mock_llm

        signal = self.router._try_level3(
            "看看数据", {}, analyst_role="procurement"
        )

        call_args = mock_llm.invoke.call_args[0][0]
        assert "采购分析师" in call_args
        assert signal.keywords[0] == "price_variance"

    def test_route_passes_analyst_role(self) -> None:
        """route() 应将 analyst_role 透传到 L3。"""
        # L1 命中，不会到 L3，role 不影响
        signal = self.router.route("分析三路匹配发票收货异常", analyst_role="finance")
        assert signal.route_level == 1  # L1 命中，不受角色影响


# ============================================================
# 非分析意图检测（方案 C）
# ============================================================


class TestNonAnalysisQueryDetection:
    """_is_non_analysis_query 前置过滤测试。"""

    # ── 应被识别为非分析意图（bypass=True）──
    @pytest.mark.parametrize("query", [
        # 明确回溯类
        "上次分析的结果呢",
        "上一次分析了什么",
        "刚才的报告呢",
        "刚刚说了什么",
        "结果呢",
        "结论是什么",
        "显示结果",
        "再说一遍",
        "重复一下",
        "帮我回忆",
        "回顾一下",
        "概括一下",
        # 歧义回溯词但无分析关键词
        "之前说的是什么",
        "前面提到的是什么",
        # 闲聊/问候
        "你好",
        "谢谢",
        "hello",
        "bye",
        # 能力询问
        "你能做什么",
        "有哪些分析功能",
        # 否定/取消
        "不需要了",
        "算了",
        "好的",
        "知道了",
        # 纯确认/追问
        "继续",
        "然后呢",
        "嗯",
    ])
    def test_bypass_queries(self, query: str) -> None:
        assert _is_non_analysis_query(query) is True

    # ── 歧义回溯词 + 分析关键词（不应 bypass）──
    @pytest.mark.parametrize("query", [
        "分析之前30天的价格差异",        # "之前"是歧义词 + 含"分析/价格/差异"
        "查看之前的采购订单数据",          # "之前"是歧义词 + 含"采购/订单"
        "前面几个月的付款合规情况",        # "前面"是歧义词 + 含"付款/合规"
        "之前SUP-001的分析有异常",        # "之前"是歧义词 + 含"分析/异常"
        "analyze previous month purchase orders",  # "previous"是歧义词 + 含分析词
    ])
    def test_ambiguous_recall_with_analysis_not_blocked(self, query: str) -> None:
        assert _is_non_analysis_query(query) is False

    # ── 明确回溯词（即使含分析词也应 bypass）──
    @pytest.mark.parametrize("query", [
        "上次分析那个供应商的价格差异",    # "上次"是明确回溯
        "帮我总结一下刚才的分析",          # "总结一下"是明确回溯
        "刚才收到一批货有质量问题",        # "刚才"是明确回溯
        "上一次分析的异常有哪些",          # "上一次"是明确回溯
    ])
    def test_clear_recall_always_bypass(self, query: str) -> None:
        assert _is_non_analysis_query(query) is True

    # ── 正常分析查询（不应 bypass）──
    @pytest.mark.parametrize("query", [
        "分析三路匹配异常",
        "检查价格差异",
        "分析付款逾期情况",
        "评估供应商 SUP-001 绩效",
        "采购支出按品类分布",
        "有没有重复发票",
        "折扣利用率分析",
        "从下单到收货要多久",
        "供应商集中度分析",
        "分析最近的收货异常",
        "最近有什么异常",
        "看看采购数据",
        "哪些订单有问题",
    ])
    def test_normal_queries_not_blocked(self, query: str) -> None:
        assert _is_non_analysis_query(query) is False

    def test_bypass_skips_l1_l2_and_l3(self) -> None:
        """前置 bypass 命中后直接生成 sentinel 信号，不再走 L1/L2/L3。"""
        router = IntentRouter(settings=Settings())
        mock_llm = MagicMock()
        # 即便 mock 了 LLM，bypass 命中后也不该被调用
        router._llm = mock_llm

        signal = router.route("上次分析的结果呢")
        assert signal.route_level == 0
        assert signal.intent_kind == IntentKind.RECALL
        mock_llm.invoke.assert_not_called()


# ============================================================
# 新版 intent_kind 二段分类
# ============================================================


class TestClassifyBypass:
    """``_classify_bypass`` 按 IntentKind 分类的前置 bypass 测试。"""

    @pytest.mark.parametrize("query", [
        "你好", "您好", "hello", "hi", "hey",
        "谢谢", "感谢", "thanks", "thank you",
        "再见", "拜拜", "bye",
        "不需要了", "算了", "取消", "好的", "知道了",
        "继续", "然后呢", "嗯",
    ])
    def test_chitchat_kind(self, query: str) -> None:
        assert _classify_bypass(query) == IntentKind.CHITCHAT

    @pytest.mark.parametrize("query", [
        "你能做什么",
        "你会什么",
        "有什么功能",
        "支持哪些采购分析",
        "可以做什么",
        "有哪些分析",
        "如何使用",
        "怎么使用这个系统",
        "help",
    ])
    def test_meta_kind(self, query: str) -> None:
        assert _classify_bypass(query) == IntentKind.META

    @pytest.mark.parametrize("query", [
        "上次分析的结果呢",
        "刚才的报告呢",
        "结果呢",
        "再说一遍",
        "总结一下",
        "之前说的是什么",  # 歧义回溯 + 无分析关键词
    ])
    def test_recall_kind(self, query: str) -> None:
        assert _classify_bypass(query) == IntentKind.RECALL

    @pytest.mark.parametrize("query", [
        "分析三路匹配异常",
        "检查价格差异",
        "查询最新的一个 PO",
        "分析之前 30 天的价格差异",  # 歧义回溯 + 含分析关键词
    ])
    def test_no_bypass(self, query: str) -> None:
        assert _classify_bypass(query) is None

    def test_chitchat_overrides_meta(self) -> None:
        """CHITCHAT 优先级高于 META（短问候比能力询问更具排他性）。"""
        # "好的" 是闲聊类否定确认，不是 META
        assert _classify_bypass("好的") == IntentKind.CHITCHAT


class TestLooksLikeDataLookup:
    """``_looks_like_data_lookup`` 启发式判定测试。"""

    @pytest.mark.parametrize("query", [
        "查询最新的一个 PO",
        "查最新的采购订单",
        "看看 SUP-001 的发票",
        "列出所有付款单",
        "show me the latest invoice",
        "list all suppliers",
        "最新的 PO 是哪一个",
        "最近的发票",
    ])
    def test_lookup_positive(self, query: str) -> None:
        assert _looks_like_data_lookup(query) is True

    @pytest.mark.parametrize("query", [
        "分析三路匹配异常",
        "今天天气怎么样",
        "你好",
        "查询",  # 仅动词，无实体
        "PO",  # 仅实体，无动词/修饰
    ])
    def test_lookup_negative(self, query: str) -> None:
        assert _looks_like_data_lookup(query) is False


class TestL1DataLookupFallback:
    """L1 在 analysis 规则全 miss 时尝试 DATA_LOOKUP 兜底。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def test_lookup_query_returns_data_lookup_signal(self) -> None:
        """'查询最新的一个 po' 应在 L1 直接判为 DATA_LOOKUP。"""
        signal = self.router._try_level1("查询最新的一个 po", {})
        assert signal is not None
        assert signal.intent_kind == IntentKind.DATA_LOOKUP
        assert signal.keywords == ["data_lookup"]
        assert signal.route_level == 1

    def test_analysis_keyword_takes_priority(self) -> None:
        """同时含 analysis 关键词与 lookup 动词时，仍优先判为 ANALYSIS。"""
        # "查询发票重复情况" 含 "查询"+"发票" lookup signal，但更优是 INVOICE_DUPLICATE
        signal = self.router._try_level1("发票重复检查", {})
        assert signal is not None
        assert signal.intent_kind == IntentKind.ANALYSIS

    def test_pure_chitchat_returns_none(self) -> None:
        """既不命中 analysis 规则也不像 lookup → None（让 L2/L3 处理）。"""
        signal = self.router._try_level1("今天天气怎么样", {})
        assert signal is None


class TestL3IntentKindBranches:
    """L3 LLM 输出不同 intent_kind 时的分支构造测试。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def _mock_llm(self, content: str) -> None:
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = content
        mock_llm.invoke.return_value = mock_response
        self.router._llm = mock_llm

    def test_intent_kind_analysis(self) -> None:
        self._mock_llm(
            '{"intent_kind":"analysis","type":"price_variance","confidence":0.9,'
            '"missing_params":[],"supplier_id":"SUP-001","po_number":null,"days":30}'
        )
        signal = self.router._try_level3("分析 SUP-001 的价格差异", {})
        assert signal.intent_kind == IntentKind.ANALYSIS
        assert signal.keywords == ["price_variance"]
        assert signal.entities.get("supplier_id") == "SUP-001"
        assert signal.entities.get("days") == 30

    def test_intent_kind_data_lookup(self) -> None:
        self._mock_llm(
            '{"intent_kind":"data_lookup","type":"","confidence":0.85,'
            '"missing_params":[],"supplier_id":null,"po_number":null,"days":null}'
        )
        signal = self.router._try_level3("查询最新的一个 PO", {})
        assert signal.intent_kind == IntentKind.DATA_LOOKUP
        assert signal.keywords == ["data_lookup"]

    def test_intent_kind_clarification_with_missing_params(self) -> None:
        self._mock_llm(
            '{"intent_kind":"clarification","type":"three_way_match","confidence":0.7,'
            '"missing_params":["time_range","supplier_id"],'
            '"supplier_id":null,"po_number":null,"days":null}'
        )
        signal = self.router._try_level3("做一下三路匹配", {})
        assert signal.intent_kind == IntentKind.CLARIFICATION
        assert signal.missing_params == ["time_range", "supplier_id"]
        # type hint 保留在 keywords 中供下游展示
        assert signal.keywords == ["three_way_match"]

    def test_intent_kind_meta(self) -> None:
        self._mock_llm(
            '{"intent_kind":"meta","type":"","confidence":0.95,'
            '"missing_params":[],"supplier_id":null,"po_number":null,"days":null}'
        )
        signal = self.router._try_level3("你支持哪些分析", {})
        assert signal.intent_kind == IntentKind.META
        assert signal.keywords == ["meta"]

    def test_intent_kind_chitchat(self) -> None:
        self._mock_llm(
            '{"intent_kind":"chitchat","type":"","confidence":0.95,'
            '"missing_params":[],"supplier_id":null,"po_number":null,"days":null}'
        )
        signal = self.router._try_level3("今天天气怎么样", {})
        assert signal.intent_kind == IntentKind.CHITCHAT
        assert signal.keywords == ["chitchat"]

    def test_intent_kind_out_of_scope(self) -> None:
        self._mock_llm(
            '{"intent_kind":"out_of_scope","type":"","confidence":0.9,'
            '"missing_params":[],"supplier_id":null,"po_number":null,"days":null}'
        )
        signal = self.router._try_level3("帮我看销售订单的回款", {})
        assert signal.intent_kind == IntentKind.OUT_OF_SCOPE
        assert signal.keywords == ["out_of_scope"]

    def test_invalid_intent_kind_falls_back_to_analysis(self) -> None:
        """LLM 返回未知 intent_kind 时降级为 ANALYSIS+COMPREHENSIVE。"""
        self._mock_llm(
            '{"intent_kind":"foo_bar","type":"","confidence":0.5,'
            '"missing_params":[],"supplier_id":null,"po_number":null,"days":null}'
        )
        signal = self.router._try_level3("查询", {})
        assert signal.intent_kind == IntentKind.ANALYSIS

    def test_missing_params_non_string_filtered(self) -> None:
        """missing_params 中的非字符串元素应被过滤。"""
        self._mock_llm(
            '{"intent_kind":"clarification","type":"","confidence":0.6,'
            '"missing_params":["time_range",null,{"foo":1}],'
            '"supplier_id":null,"po_number":null,"days":null}'
        )
        signal = self.router._try_level3("分析", {})
        assert signal.intent_kind == IntentKind.CLARIFICATION
        assert "time_range" in signal.missing_params
        # null 与 dict 应被丢弃
        assert all(isinstance(p, str) for p in signal.missing_params)


class TestRouteEndToEndIntentKinds:
    """``route()`` 在不同 intent_kind 下的完整行为。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def test_chitchat_bypass_no_l1_l2_l3(self) -> None:
        """CHITCHAT bypass 不调 LLM 也不查 L2 store。"""
        mock_llm = MagicMock()
        mock_store = MagicMock()
        self.router._llm = mock_llm
        self.router._seeds_store = mock_store
        self.router._seeds_loaded = True

        signal = self.router.route("你好")
        assert signal.intent_kind == IntentKind.CHITCHAT
        assert signal.route_level == 0
        mock_llm.invoke.assert_not_called()
        mock_store.search.assert_not_called()

    def test_meta_bypass_no_l1_l2_l3(self) -> None:
        mock_llm = MagicMock()
        self.router._llm = mock_llm

        signal = self.router.route("你能做什么")
        assert signal.intent_kind == IntentKind.META
        assert signal.route_level == 0
        mock_llm.invoke.assert_not_called()

    def test_recall_bypass_no_l1_l2_l3(self) -> None:
        mock_llm = MagicMock()
        self.router._llm = mock_llm

        signal = self.router.route("上次的结果呢")
        assert signal.intent_kind == IntentKind.RECALL
        assert signal.route_level == 0
        mock_llm.invoke.assert_not_called()

    def test_lookup_query_l1_hits_data_lookup(self) -> None:
        """'查询最新的一个 po' 应在 L1 直接判为 DATA_LOOKUP，不进 L2/L3。"""
        mock_llm = MagicMock()
        self.router._llm = mock_llm
        # 模拟 L2 store 永远空，避免误命中
        mock_store = MagicMock()
        mock_store.search.return_value = []
        self.router._seeds_store = mock_store
        self.router._seeds_loaded = True

        signal = self.router.route("查询最新的一个 po")
        assert signal.intent_kind == IntentKind.DATA_LOOKUP
        assert signal.route_level == 1
        mock_llm.invoke.assert_not_called()


class TestL2SentinelCategoryMatching:
    """L2 种子库匹配 data_lookup / meta 类目时构造对应 IntentKind。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def test_l2_data_lookup_seed(self) -> None:
        mock_store = MagicMock()
        mock_store.search.return_value = [
            {
                "id": "seed_data_lookup_0",
                "text": "查询最新的一个 PO",
                "metadata": {"analysis_type": "data_lookup"},
                "distance": 0.1,
            }
        ]
        self.router._seeds_store = mock_store
        self.router._seeds_loaded = True

        signal = self.router._try_level2("查最新 PO", {})
        assert signal is not None
        assert signal.intent_kind == IntentKind.DATA_LOOKUP
        assert signal.keywords == ["data_lookup"]
        assert signal.route_level == 2

    def test_l2_meta_seed(self) -> None:
        mock_store = MagicMock()
        mock_store.search.return_value = [
            {
                "id": "seed_meta_0",
                "text": "你能做什么",
                "metadata": {"analysis_type": "meta"},
                "distance": 0.05,
            }
        ]
        self.router._seeds_store = mock_store
        self.router._seeds_loaded = True

        signal = self.router._try_level2("能做啥", {})
        assert signal is not None
        assert signal.intent_kind == IntentKind.META
        assert signal.keywords == ["meta"]
