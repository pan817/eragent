"""IntentRouter 路由单元测试。"""

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
)
from core.orchestrator.signal import IntentKind, QuerySignal


# ============================================================
# _extract_params 参数提取测试
# ============================================================


class TestExtractParams:
    """正则参数提取（复用原 IntentParser 逻辑）。"""

    def test_extract_supplier_id(self) -> None:
        params = _extract_params("查看 SUP-001 的采购订单")
        assert params["vendor_id"] == "SUP-001"

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
        assert params["invoice_num"] == "INV-2024-0001"

    def test_extract_payment_number(self) -> None:
        params = _extract_params("查询付款单 PAY-2024-0500")
        assert params["check_number"] == "PAY-2024-0500"

    def test_extract_receipt_number(self) -> None:
        params = _extract_params("收货单 RCV-2024-0100 有问题")
        assert params["receipt_number"] == "RCV-2024-0100"

    def test_extract_multiple_entities(self) -> None:
        params = _extract_params("PO-001 的发票 INV-001 付款 PAY-001 来自 SUP-001")
        assert params.get("po_number") == "PO-001"
        assert params.get("invoice_num") == "INV-001"
        assert params.get("check_number") == "PAY-001"
        assert params.get("vendor_id") == "SUP-001"

    def test_no_params(self) -> None:
        params = _extract_params("给我一份分析报告")
        assert params == {}


# ============================================================
# Level 1：关键词命中率测试
# ============================================================


class TestParseIntegration:
    """parse() / route() 方法端到端测试（mock 统一 LLM）。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def _mock_unified(self, response_json: str) -> None:
        """注入 mock UnifiedRouter。"""
        from core.orchestrator.unified_router import UnifiedRouter, _parse_unified_response

        mock_ur = MagicMock(spec=UnifiedRouter)
        mock_ur.route.side_effect = lambda query, **kw: _parse_unified_response(
            response_json, query,
        )
        self.router._unified_router = mock_ur

    def test_unified_llm_analysis(self) -> None:
        """统一 LLM 返回 analysis 类型。"""
        self._mock_unified(
            '{"intent_kind":"analysis","type":"three_way_match","confidence":0.9,'
            '"is_cross_entity":false,"resolved_query":"分析三路匹配","missing_params":[],'
            '"po_number":null,"vendor_id":null,"invoice_num":null,'
            '"check_number":null,"receipt_number":null,"days":null,"limit":null,"order_by":null}'
        )
        signal = self.router.route("分析三路匹配")
        assert signal.intent_kind == IntentKind.ANALYSIS
        assert signal.keywords == ["three_way_match"]
        assert signal.confidence == 0.9

    def test_unified_llm_data_lookup(self) -> None:
        """统一 LLM 返回 data_lookup 类型。"""
        self._mock_unified(
            '{"intent_kind":"data_lookup","type":"","confidence":0.9,'
            '"is_cross_entity":false,"resolved_query":"查最新PO","missing_params":[],'
            '"po_number":null,"vendor_id":null,"invoice_num":null,'
            '"check_number":null,"receipt_number":null,"days":null,"limit":1,"order_by":"date_desc"}'
        )
        signal = self.router.route("查最新的一个PO")
        assert signal.intent_kind == IntentKind.DATA_LOOKUP
        assert signal.entities.get("limit") == 1
        assert signal.entities.get("order_by") == "date_desc"

    def test_unified_llm_cross_entity(self) -> None:
        """统一 LLM 识别跨实体查询。"""
        self._mock_unified(
            '{"intent_kind":"data_lookup","type":"","confidence":0.85,'
            '"is_cross_entity":true,"resolved_query":"支付单 PAY-001 对应的PO","missing_params":[],'
            '"po_number":null,"vendor_id":null,"invoice_num":null,'
            '"check_number":"PAY-001","receipt_number":null,"days":null,"limit":null,"order_by":null}'
        )
        signal = self.router.route("这个支付单对应的PO")
        assert signal.is_cross_entity is True
        assert signal.entities.get("check_number") == "PAY-001"

    def test_unified_llm_with_params(self) -> None:
        """统一 LLM 提取参数。"""
        self._mock_unified(
            '{"intent_kind":"analysis","type":"price_variance","confidence":0.95,'
            '"is_cross_entity":false,"resolved_query":"分析SUP-001价格差异","missing_params":[],'
            '"po_number":null,"vendor_id":"SUP-001","invoice_num":null,'
            '"check_number":null,"receipt_number":null,"days":60,"limit":null,"order_by":null}'
        )
        signal = self.router.route("分析 SUP-001 最近60天的价格差异")
        assert signal.entities.get("vendor_id") == "SUP-001"
        assert signal.time_range_days == 60

    def test_bypass_chitchat_skips_llm(self) -> None:
        """CHITCHAT bypass 不调用统一 LLM。"""
        signal = self.router.route("你好")
        assert signal.intent_kind == IntentKind.CHITCHAT
        assert signal.route_level == 0  # bypass level

    def test_bypass_meta_skips_llm(self) -> None:
        """META bypass 不调用统一 LLM。"""
        signal = self.router.route("你能做什么")
        assert signal.intent_kind == IntentKind.META
        assert signal.route_level == 0

    def test_regex_fallback_for_entities(self) -> None:
        """统一 LLM 未提取到的实体由 regex 兜底。"""
        self._mock_unified(
            '{"intent_kind":"analysis","type":"three_way_match","confidence":0.85,'
            '"is_cross_entity":false,"resolved_query":"分析SUP-001三路匹配","missing_params":[],'
            '"po_number":null,"vendor_id":null,"invoice_num":null,'
            '"check_number":null,"receipt_number":null,"days":null,"limit":null,"order_by":null}'
        )
        # query 中有 SUP-001，LLM 没提取到，regex 兜底
        signal = self.router.route("分析 SUP-001 三路匹配")
        assert signal.entities.get("vendor_id") == "SUP-001"


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
            entities={"vendor_id": "SUP-001"},
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


class TestAnalystRoleInjection:
    """analyst_role 角色注入测试。"""

    def setup_method(self) -> None:
        self.router = IntentRouter(settings=Settings())

    def test_route_passes_analyst_role(self) -> None:
        """route() 应将 analyst_role 透传到统一 LLM。"""
        from core.orchestrator.unified_router import UnifiedRouter

        mock_ur = MagicMock(spec=UnifiedRouter)
        mock_ur.route.return_value = QuerySignal(
            raw_query="分析三路匹配",
            intent_kind=IntentKind.ANALYSIS,
            keywords=["three_way_match"],
            route_level=3,
            confidence=0.9,
        )
        self.router._unified_router = mock_ur

        self.router.route("分析三路匹配", analyst_role="finance")
        # 验证 analyst_role 被传递到 UnifiedRouter
        mock_ur.route.assert_called_once()
        call_kwargs = mock_ur.route.call_args
        assert call_kwargs.kwargs.get("analyst_role") == "finance"


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

    # ── 明确回溯词 + 无强分析意图 → 仍应 bypass ──
    @pytest.mark.parametrize("query", [
        "帮我总结一下刚才的分析",          # "总结一下"是明确回溯，无强分析意图词
        "刚才收到一批货有质量问题",        # "刚才"是明确回溯，无强分析意图词
        "上一次分析的异常有哪些",          # "上一次"是明确回溯，"异常"非强意图词
    ])
    def test_clear_recall_without_strong_intent_bypass(self, query: str) -> None:
        assert _is_non_analysis_query(query) is True

    # ── 明确回溯词 + 有强分析意图 → 不应 bypass，走 L1/L2/L3 ──
    @pytest.mark.parametrize("query", [
        "上次分析那个供应商的价格差异",    # "上次" + "价格差异"(强意图)
        "上次的三路匹配结果不对，再分析一下",  # "上次" + "三路匹配"(强意图)
        "刚才的ppv分析有问题",            # "刚才" + "ppv"(强意图)
    ])
    def test_clear_recall_with_strong_intent_not_bypass(self, query: str) -> None:
        assert _is_non_analysis_query(query) is False

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
        "上次的三路匹配再分析一下",  # 明确回溯 + 强分析意图 → 不 bypass
        "刚才那个ppv有问题重新分析",  # 明确回溯 + 强分析意图 → 不 bypass
    ])
    def test_no_bypass(self, query: str) -> None:
        assert _classify_bypass(query) is None

    def test_chitchat_overrides_meta(self) -> None:
        """CHITCHAT 优先级高于 META（短问候比能力询问更具排他性）。"""
        # "好的" 是闲聊类否定确认，不是 META
        assert _classify_bypass("好的") == IntentKind.CHITCHAT


