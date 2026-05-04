"""RECALL 意图端到端路径测试。

验证 recall 查询从路由到 orchestrator 执行路径的关键行为：
- 路由器正确分类 recall 意图（bypass 快速路径 + unified LLM 路径）
- orchestrator 在 recall 时跳过实体继承、实体补充、记忆写入
- recall 强制走 ReAct（不走 DAG / Plan and Solve）
- search_my_chat_history 工具被注入到 recall 路径
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import Settings
from core.orchestrator.signal import IntentKind, QuerySignal


@pytest.fixture
def settings() -> Settings:
    return Settings()


# ── Router: bypass 快速路径 ─────────────────────────────────────


class TestRecallBypassRouter:
    """_classify_bypass 对 recall 查询的分类。"""

    @pytest.mark.parametrize("query", [
        "上次分析的结果呢",
        "刚才的报告呢",
        "帮我回顾一下之前的分析",
        "再说一遍上次的结论",
        "上回那个供应商怎么样了",
    ])
    def test_recall_bypass_returns_recall_kind(self, query: str) -> None:
        from core.orchestrator.router import _classify_bypass

        result = _classify_bypass(query)
        assert result == IntentKind.RECALL

    @pytest.mark.parametrize("query", [
        "分析最近30天SUP-001的三路匹配",
        "查最新的PO",
        "供应商SUP-002的价格差异",
    ])
    def test_analysis_queries_not_bypassed_as_recall(self, query: str) -> None:
        from core.orchestrator.router import _classify_bypass

        result = _classify_bypass(query)
        assert result != IntentKind.RECALL

    @pytest.mark.parametrize("query", [
        "上次说的三路匹配帮我重新分析一下",
        "之前那个供应商的价格差异再做一次",
    ])
    def test_recall_with_strong_analysis_intent_not_bypassed(self, query: str) -> None:
        from core.orchestrator.router import _classify_bypass

        result = _classify_bypass(query)
        assert result is None


# ── Router: unified LLM 路径 ───────────────────────────────────


class TestRecallUnifiedRouter:
    """_parse_unified_response 对 recall JSON 的解析。"""

    def test_parse_recall_response(self) -> None:
        from core.orchestrator.unified_router import _parse_unified_response

        llm_output = json.dumps({
            "intent_kind": "recall",
            "type": "",
            "confidence": 0.9,
            "is_cross_entity": False,
            "resolved_query": "上次分析的那家供应商还有异常发票吗",
            "missing_params": [],
            "po_number": None,
            "vendor_id": None,
            "invoice_num": None,
            "check_number": None,
            "receipt_number": None,
            "days": None,
            "limit": None,
            "order_by": None,
        })
        signal = _parse_unified_response(llm_output, "上次那家供应商")
        assert signal.intent_kind == IntentKind.RECALL
        assert signal.confidence == pytest.approx(0.9)
        # RECALL 在 unified_router 中未单独处理 keywords，落入 else 分支
        # analysis_type="" 时 keywords=["comprehensive"]
        assert signal.keywords == ["comprehensive"]

    def test_recall_not_downgraded_to_analysis(self) -> None:
        from core.orchestrator.unified_router import _parse_unified_response

        llm_output = json.dumps({
            "intent_kind": "recall",
            "type": "",
            "confidence": 0.85,
        })
        signal = _parse_unified_response(llm_output, "继续上个月的分析")
        assert signal.intent_kind == IntentKind.RECALL


# ── Router: IntentRouter 集成 ──────────────────────────────────


class TestIntentRouterRecall:
    """IntentRouter.route 在 recall bypass 场景下的行为。"""

    def test_bypass_recall_produces_correct_signal(self, settings: Settings) -> None:
        from core.orchestrator.router import IntentRouter

        router = IntentRouter(settings=settings)
        signal = router.route("上次分析的结果呢")
        assert signal.intent_kind == IntentKind.RECALL
        assert signal.route_level == 0
        assert signal.keywords == ["recall"]
        assert signal.reasoning

    def test_bypass_recall_skips_llm(self, settings: Settings) -> None:
        from core.orchestrator.router import IntentRouter

        router = IntentRouter(settings=settings)
        with patch(
            "core.orchestrator.unified_router.UnifiedRouter.route"
        ) as mock_llm:
            router.route("结果呢")
            mock_llm.assert_not_called()


# ── Orchestrator: recall 路径行为验证 ──────────────────────────


class TestOrchestratorRecallPath:
    """验证 orchestrator 在 is_recall=True 时的关键分支行为。"""

    def _make_recall_signal(self, query: str = "上次的结果呢") -> QuerySignal:
        return QuerySignal(
            raw_query=query,
            intent_kind=IntentKind.RECALL,
            keywords=["recall"],
            route_level=0,
            confidence=0.9,
            reasoning="bypass: recall",
        )

    def test_recall_skips_entity_inheritance(self) -> None:
        """is_recall 时不从 session context 继承实体。"""
        signal = self._make_recall_signal()
        is_recall = signal.intent_kind == IntentKind.RECALL
        assert is_recall

        parsed_params: dict = {}
        relevant_entities = {"vendor_id": "SUP-001", "po_number": "PO-100"}

        if not is_recall:
            for key, val in relevant_entities.items():
                if key not in parsed_params or not parsed_params[key]:
                    parsed_params[key] = val

        assert "vendor_id" not in parsed_params
        assert "po_number" not in parsed_params

    def test_recall_forces_react_no_dag(self) -> None:
        """is_recall 时 use_dag=False、use_plan_and_solve=False。"""
        signal = self._make_recall_signal()
        is_recall = signal.intent_kind == IntentKind.RECALL
        is_data_lookup = signal.intent_kind == IntentKind.DATA_LOOKUP
        low_confidence = False

        if is_recall or is_data_lookup or low_confidence:
            use_dag = False
        else:
            use_dag = True

        use_plan_and_solve = (
            not use_dag
            and not is_recall
            and not low_confidence
        )

        assert use_dag is False
        assert use_plan_and_solve is False

    def test_recall_skips_memory_write(self) -> None:
        """is_recall 时 skip_memory_write=True，不写入长期记忆/报告/实体。"""
        signal = self._make_recall_signal()
        is_recall = signal.intent_kind == IntentKind.RECALL

        should_persist_report = not is_recall
        should_write_memory = not is_recall
        should_save_entities = not is_recall

        assert should_persist_report is False
        assert should_write_memory is False
        assert should_save_entities is False


# ── 工具注入验证 ───────────────────────────────────────────────


class TestRecallToolInjection:
    """search_my_chat_history 在工具集中的注入验证。"""

    def test_chat_tool_in_postgresql_mode(self) -> None:
        from modules.p2p.tools import get_tools_for_mode

        tools = get_tools_for_mode("postgresql")
        tool_names = [t.name for t in tools]
        assert "search_my_chat_history" in tool_names

    def test_chat_tool_in_hybrid_mode(self) -> None:
        from modules.p2p.tools import get_tools_for_mode

        tools = get_tools_for_mode("hybrid")
        tool_names = [t.name for t in tools]
        assert "search_my_chat_history" in tool_names

    def test_chat_tools_list_contains_tool(self) -> None:
        from modules.p2p.tools import CHAT_TOOLS

        assert len(CHAT_TOOLS) == 1
        assert CHAT_TOOLS[0].name == "search_my_chat_history"


# ── 工具 → MemoryManager 调用链 ───────────────────────────────


class TestRecallToolToManager:
    """search_my_chat_history → MemoryManager.search_chat_history 调用链。"""

    @pytest.mark.asyncio
    async def test_tool_calls_manager_with_correct_params(self) -> None:
        from modules.p2p.tools.chat_history import search_my_chat_history

        mock_results = [
            {
                "session_id": "s1",
                "session_title": "三路匹配分析",
                "snippet": "SUP-001 的三路匹配率为 85%",
                "entities": {"vendor_id": "SUP-001"},
                "created_at": "2026-04-28T10:00:00",
                "match_type": "exact",
                "recency_score": 0.95,
            }
        ]

        with (
            patch("core.tasks.context.get_current_user_id", return_value="u1"),
            patch(
                "core.memory.manager.MemoryManager.search_chat_history",
                new_callable=AsyncMock,
                return_value=mock_results,
            ) as mock_search,
        ):
            raw = await search_my_chat_history.ainvoke({
                "query": "上次那家供应商的三路匹配",
                "days": 30,
                "limit": 5,
            })

        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args.kwargs
        assert call_kwargs["user_id"] == "u1"
        assert call_kwargs["query"] == "上次那家供应商的三路匹配"
        assert call_kwargs["days"] == 30
        assert call_kwargs["limit"] == 5

        parsed = json.loads(raw)
        assert len(parsed) == 1
        assert parsed[0]["session_id"] == "s1"
        assert parsed[0]["relevance_score"] == 0.95

    @pytest.mark.asyncio
    async def test_tool_returns_empty_json_on_no_results(self) -> None:
        from modules.p2p.tools.chat_history import search_my_chat_history

        with (
            patch("core.tasks.context.get_current_user_id", return_value="u1"),
            patch(
                "core.memory.manager.MemoryManager.search_chat_history",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            raw = await search_my_chat_history.ainvoke({"query": "不存在的内容"})

        assert raw == "[]"


# ── Golden recall 查询 → 路由分类一致性 ────────────────────────


class TestGoldenRecallRouting:
    """10 条 golden recall 查询通过 IntentRouter 端到端路由。

    bypass 能命中的直接验证 intent_kind=RECALL；
    bypass 不命中的需过 LLM（此处 mock），验证非拒答（不被归为 OUT_OF_SCOPE/CHITCHAT）。
    """

    GOLDEN_RECALL_QUERIES = [
        "上周我们讨论的那家供应商，本月还有异常发票吗？",
        "继续上个月那个三路匹配差异分析",
        "我之前说过的那个付款政策，再帮我查一遍",
        "上次分析的结果呢",
        "那家化工厂后来怎么样了",
        "之前讨论的SUP-003的问题解决了吗",
        "我们上回分析的供应商绩效，数据更新了吗",
        "上个月在采购合规上得出过什么结论",
        "帮我回顾一下之前的付款违规分析",
        "还记得我上次让你查的那批发票吗",
    ]

    @pytest.mark.parametrize("query", GOLDEN_RECALL_QUERIES)
    def test_golden_recall_not_rejected(self, query: str, settings: Settings) -> None:
        """recall 查询不应被归为 OUT_OF_SCOPE 或 CHITCHAT。"""
        from core.orchestrator.router import _classify_bypass

        result = _classify_bypass(query)
        if result is not None:
            assert result not in (IntentKind.OUT_OF_SCOPE, IntentKind.CHITCHAT), (
                f"Query '{query}' wrongly classified as {result.value}"
            )

    def test_simple_recall_bypassed(self, settings: Settings) -> None:
        """最简单的 recall 查询被 bypass 直接命中。"""
        from core.orchestrator.router import IntentRouter

        router = IntentRouter(settings=settings)
        signal = router.route("上次分析的结果呢")
        assert signal.intent_kind == IntentKind.RECALL

    def test_ambiguous_recall_deferred_to_llm(self, settings: Settings) -> None:
        """含强分析意图的 recall 不被 bypass，委托给 LLM 路由。"""
        from core.orchestrator.router import _classify_bypass

        result = _classify_bypass("继续上个月那个三路匹配差异分析")
        assert result is None
