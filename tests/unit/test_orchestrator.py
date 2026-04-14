"""Orchestrator 编排器单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.schemas.analysis import (
    AnalysisRequest,
    AnalysisStatus,
    AnalysisType,
)
from config.settings import Settings
from core.orchestrator.orchestrator import Orchestrator
from core.orchestrator.signal import QuerySignal


def _make_mock_agent(**run_kwargs: object) -> MagicMock:
    """创建带 AsyncMock run 方法的 mock agent。"""
    agent = MagicMock()
    agent.run = AsyncMock(**run_kwargs)
    return agent


def _make_l3_signal(query: str = "测试查询") -> QuerySignal:
    """创建一个 Level 3 的 QuerySignal（强制走 ReAct 路径）。"""
    return QuerySignal(
        raw_query=query,
        keywords=[AnalysisType.COMPREHENSIVE.value],
        entities={},
        route_level=3,
        confidence=0.5,
        reasoning="L3 测试",
    )


@pytest.fixture()
def settings() -> Settings:
    return Settings()


class TestOrchestratorInit:
    """初始化测试。"""

    def test_init_with_settings(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        assert orch._settings is settings

    def test_init_without_settings(self) -> None:
        mock_settings = Settings()
        with patch("core.orchestrator.orchestrator.get_settings", return_value=mock_settings):
            orch = Orchestrator()
        assert orch._settings is mock_settings

    def test_lazy_agent_property(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        assert orch._agent is None

        mock_p2p = MagicMock()
        with patch("modules.p2p.agent.P2PAgent", return_value=mock_p2p):
            agent = orch._lazy_agent
        assert agent is mock_p2p
        assert orch._lazy_agent is mock_p2p


class TestOrchestratorReActPath:
    """ReAct 路径测试（Level 3 兜底）。"""

    @pytest.mark.asyncio
    async def test_react_success(self, settings: Settings) -> None:
        """Level 3 走 ReAct 路径应成功，且 summary 包含路由监控信息。"""
        orch = Orchestrator(settings=settings)
        orch._agent = _make_mock_agent(return_value={
            "anomalies": [],
            "supplier_kpis": [],
            "summary": {"total": 0},
            "report_markdown": "# Report",
            "completed_tasks": ["analysis"],
            "failed_tasks": [],
        })

        # 强制 L3 路由
        with patch.object(orch._intent_router, "route", return_value=_make_l3_signal()):
            request = AnalysisRequest(query="复杂的探索性分析问题")
            result = await orch.analyze(request)

        assert result.status == AnalysisStatus.SUCCESS
        assert result.report_markdown == "# Report"
        # 路由监控信息应被注入 summary
        assert result.summary.get("route_type") == "ReAct"
        assert result.summary.get("route_level") == 3
        assert result.summary.get("route_confidence") == 0.5
        # agent 原始 summary 也应保留
        assert result.summary.get("total") == 0

    @pytest.mark.asyncio
    async def test_react_with_explicit_type(self, settings: Settings) -> None:
        """显式指定 analysis_type 时应优先使用。"""
        orch = Orchestrator(settings=settings)
        orch._agent = _make_mock_agent(return_value={
            "anomalies": [], "summary": {}, "report_markdown": "",
            "completed_tasks": [], "failed_tasks": [],
        })

        signal = _make_l3_signal()
        with patch.object(orch._intent_router, "route", return_value=signal):
            request = AnalysisRequest(
                query="分析",
                analysis_type=AnalysisType.PRICE_VARIANCE,
            )
            result = await orch.analyze(request)

        assert result.analysis_type == AnalysisType.PRICE_VARIANCE

    @pytest.mark.asyncio
    async def test_react_with_time_range(self, settings: Settings) -> None:
        """指定 time_range_days 应传递给 Agent。"""
        orch = Orchestrator(settings=settings)
        orch._agent = _make_mock_agent(return_value={
            "anomalies": [], "summary": {}, "report_markdown": "",
            "completed_tasks": [], "failed_tasks": [],
        })

        with patch.object(orch._intent_router, "route", return_value=_make_l3_signal()):
            request = AnalysisRequest(query="分析", time_range_days=60)
            result = await orch.analyze(request)

        assert "60" in result.time_range

    @pytest.mark.asyncio
    async def test_react_failure(self, settings: Settings) -> None:
        """Agent 异常时应返回 FAILED。"""
        orch = Orchestrator(settings=settings)
        orch._agent = _make_mock_agent(side_effect=RuntimeError("boom"))

        with patch.object(orch._intent_router, "route", return_value=_make_l3_signal()):
            request = AnalysisRequest(query="测试")
            result = await orch.analyze(request)

        assert result.status == AnalysisStatus.FAILED
        assert result.error is not None
        assert "boom" in result.error.message

    @pytest.mark.asyncio
    async def test_react_with_session_id(self, settings: Settings) -> None:
        """请求中的 session_id 应被使用。"""
        orch = Orchestrator(settings=settings)
        orch._agent = _make_mock_agent(return_value={
            "anomalies": [], "summary": {}, "report_markdown": "",
            "completed_tasks": [], "failed_tasks": [],
        })

        with patch.object(orch._intent_router, "route", return_value=_make_l3_signal()):
            request = AnalysisRequest(query="分析", session_id="my-session")
            result = await orch.analyze(request)

        assert result.session_id == "my-session"


class TestOrchestratorUnknownIntent:
    """L3 判定为 unknown（非采购分析）时的早退出分支。"""

    @pytest.mark.asyncio
    async def test_unknown_intent_early_return(self, settings: Settings) -> None:
        """signal.keywords==['unknown'] 且未指定 analysis_type 时应早返回，不触发 agent。"""
        orch = Orchestrator(settings=settings)
        mock_agent = _make_mock_agent(return_value={})
        orch._agent = mock_agent

        unknown_signal = QuerySignal(
            raw_query="天气怎么样",
            keywords=["unknown"],
            entities={},
            route_level=3,
            confidence=0.7,
            reasoning="L3 判定为非 ERP 采购分析意图（unknown）",
        )

        with patch.object(orch._intent_router, "route", return_value=unknown_signal):
            request = AnalysisRequest(query="今天天气怎么样")
            result = await orch.analyze(request)

        # 成功返回但不触发 agent
        assert result.status == AnalysisStatus.SUCCESS
        mock_agent.run.assert_not_called()
        # 报告说明不属于分析范围
        assert "采购分析" in result.report_markdown
        # summary 打标非分析
        assert result.summary.get("route_type") == "non_analysis"

    @pytest.mark.asyncio
    async def test_unknown_intent_overridden_by_explicit_type(
        self, settings: Settings
    ) -> None:
        """用户显式指定 analysis_type 时，即使 L3 判 unknown 也应以用户意图为准。"""
        orch = Orchestrator(settings=settings)
        orch._agent = _make_mock_agent(return_value={
            "anomalies": [], "supplier_kpis": [], "summary": {},
            "report_markdown": "# Forced", "completed_tasks": [], "failed_tasks": [],
        })

        unknown_signal = QuerySignal(
            raw_query="x",
            keywords=["unknown"],
            entities={},
            route_level=3,
            confidence=0.6,
            reasoning="L3 unknown",
        )

        with patch.object(orch._intent_router, "route", return_value=unknown_signal):
            request = AnalysisRequest(
                query="x", analysis_type=AnalysisType.THREE_WAY_MATCH
            )
            result = await orch.analyze(request)

        # 未早退出，走完整流程
        assert result.report_markdown == "# Forced"


class TestReferenceResolution:
    """指代消解测试。"""

    def test_po_reference(self) -> None:
        """'这个po' 应只关联 po_number。"""
        ctx = {"has_history": True, "entities": {"po_number": "PO-001", "supplier_id": "SUP-001"}}
        enhanced, entities = Orchestrator._resolve_references("分析这个po的风险", ctx)
        assert "PO-001" in enhanced
        assert "po_number" in entities
        assert "supplier_id" not in entities

    def test_supplier_reference(self) -> None:
        """'这个供应商' 应只关联 supplier_id。"""
        ctx = {"has_history": True, "entities": {"po_number": "PO-001", "supplier_id": "SUP-001"}}
        enhanced, entities = Orchestrator._resolve_references("评估这个供应商的绩效", ctx)
        assert "SUP-001" in enhanced
        assert "supplier_id" in entities
        assert "po_number" not in entities

    def test_no_reference(self) -> None:
        """无指代词时不做隐式继承。"""
        ctx = {"has_history": True, "entities": {"po_number": "PO-001", "supplier_id": "SUP-001"}}
        enhanced, entities = Orchestrator._resolve_references("分析价格差异", ctx)
        assert enhanced == "分析价格差异"
        assert entities == {}  # 无指代词，不继承任何实体

    def test_no_history(self) -> None:
        """无历史时原样返回。"""
        ctx = {"has_history": False}
        enhanced, entities = Orchestrator._resolve_references("分析这个po", ctx)
        assert enhanced == "分析这个po"
        assert entities == {}

    def test_generic_reference(self) -> None:
        """'它的'等通用指代应全部补充。"""
        ctx = {"has_history": True, "entities": {"po_number": "PO-001", "supplier_id": "SUP-001"}}
        enhanced, entities = Orchestrator._resolve_references("它的情况怎么样", ctx)
        assert "po_number" in entities
        assert "supplier_id" in entities

    def test_order_reference(self) -> None:
        """'该订单' 应关联 po_number。"""
        ctx = {"has_history": True, "entities": {"po_number": "PO-002"}}
        enhanced, entities = Orchestrator._resolve_references("检查该订单是否有异常", ctx)
        assert "PO-002" in enhanced
        assert entities.get("po_number") == "PO-002"

    def test_payment_reference(self) -> None:
        """'这个支付单' 应只关联 payment_number。"""
        ctx = {
            "has_history": True,
            "entities": {"payment_number": "PAY-001", "supplier_id": "SUP-001", "po_number": "PO-001"},
        }
        enhanced, entities = Orchestrator._resolve_references("分析这个支付单的合规性", ctx)
        assert "PAY-001" in enhanced
        assert "payment_number" in entities
        assert "supplier_id" not in entities
        assert "po_number" not in entities

    def test_invoice_reference(self) -> None:
        """'这个发票' 应只关联 invoice_number。"""
        ctx = {
            "has_history": True,
            "entities": {"invoice_number": "INV-001", "supplier_id": "SUP-001"},
        }
        enhanced, entities = Orchestrator._resolve_references("检查这个发票的匹配情况", ctx)
        assert "INV-001" in enhanced
        assert "invoice_number" in entities
        assert "supplier_id" not in entities

    def test_receipt_reference(self) -> None:
        """'该收货单' 应只关联 receipt_number。"""
        ctx = {"has_history": True, "entities": {"receipt_number": "RCV-001"}}
        enhanced, entities = Orchestrator._resolve_references("该收货单的数量对吗", ctx)
        assert "RCV-001" in enhanced
        assert entities.get("receipt_number") == "RCV-001"

    def test_quantifier_payment_reference(self) -> None:
        """'这笔付款' 应关联 payment_number。"""
        ctx = {"has_history": True, "entities": {"payment_number": "PAY-001", "supplier_id": "SUP-001"}}
        enhanced, entities = Orchestrator._resolve_references("这笔付款有问题吗", ctx)
        assert "PAY-001" in enhanced
        assert "payment_number" in entities
        assert "supplier_id" not in entities

    def test_quantifier_invoice_reference(self) -> None:
        """'这张发票' 应关联 invoice_number。"""
        ctx = {"has_history": True, "entities": {"invoice_number": "INV-001"}}
        enhanced, entities = Orchestrator._resolve_references("这张发票金额不对", ctx)
        assert "INV-001" in enhanced
        assert entities.get("invoice_number") == "INV-001"

    def test_temporal_reference(self) -> None:
        """'刚才的付款单' 应关联 payment_number。"""
        ctx = {"has_history": True, "entities": {"payment_number": "PAY-002"}}
        enhanced, entities = Orchestrator._resolve_references("刚才的付款单有没有逾期", ctx)
        assert "PAY-002" in enhanced
        assert entities.get("payment_number") == "PAY-002"

    def test_above_mentioned_reference(self) -> None:
        """'上面提到的供应商' 应关联 supplier_id。"""
        ctx = {"has_history": True, "entities": {"supplier_id": "SUP-003", "po_number": "PO-001"}}
        enhanced, entities = Orchestrator._resolve_references("上面提到的供应商绩效怎么样", ctx)
        assert "SUP-003" in enhanced
        assert "supplier_id" in entities
        assert "po_number" not in entities


class TestOrchestratorSessionContext:
    """会话上下文读取测试。"""

    def test_load_context_no_checkpointer(self, settings: Settings) -> None:
        """checkpointer 不可用时应返回空上下文。"""
        orch = Orchestrator(settings=settings)
        orch._checkpointer = None

        ctx = orch._load_session_context("s1")
        assert ctx["has_history"] is False

    def test_load_context_no_history(self, settings: Settings) -> None:
        """无历史时返回空上下文。"""
        orch = Orchestrator(settings=settings)
        mock_checkpointer = MagicMock()
        mock_checkpointer.get_tuple.return_value = None
        orch._checkpointer = mock_checkpointer

        ctx = orch._load_session_context("s1")
        assert ctx["has_history"] is False

    def test_load_context_with_history(self, settings: Settings) -> None:
        """有历史时应提取实体。"""
        from langchain_core.messages import AIMessage, HumanMessage

        orch = Orchestrator(settings=settings)
        mock_checkpointer = MagicMock()

        # 模拟 checkpointer 返回历史
        mock_tuple = MagicMock()
        mock_tuple.checkpoint = {
            "channel_values": {
                "messages": [
                    HumanMessage(content="查询 PO-2024-0035 的数据"),
                    AIMessage(content="PO-2024-0035 的采购订单金额为 100000 元，供应商 SUP-001"),
                ]
            }
        }
        mock_checkpointer.get_tuple.return_value = mock_tuple
        orch._checkpointer = mock_checkpointer

        ctx = orch._load_session_context("s1")
        assert ctx["has_history"] is True
        assert ctx["entities"].get("po_number") is not None
        assert ctx["entities"].get("supplier_id") is not None

    def test_load_context_failure_not_blocking(self, settings: Settings) -> None:
        """checkpointer 读取异常不应阻塞。"""
        orch = Orchestrator(settings=settings)
        mock_checkpointer = MagicMock()
        mock_checkpointer.get_tuple.side_effect = RuntimeError("db error")
        orch._checkpointer = mock_checkpointer

        ctx = orch._load_session_context("s1")
        assert ctx["has_history"] is False

    @pytest.mark.asyncio
    async def test_context_po_entity_triggers_dag(self, settings: Settings) -> None:
        """'分析这个po' + 上下文有 PO 号 → 指代消解后走 PO 综合 DAG。"""
        orch = Orchestrator(settings=settings)

        # Mock DAG executor
        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value={
            "status": "ok",
            "outputs": {"report": "# PO Risk"},
            "completed_tasks": ["t1"],
            "failed_tasks": {},
            "report": "# PO Risk",
            "duration_sec": 1.0,
        })
        mock_executor._registry = MagicMock()
        orch._dag_executor = mock_executor

        orch._checkpointer = None

        # Mock session context 返回 PO 号
        with patch.object(
            orch, "_load_session_context",
            return_value={
                "has_history": True,
                "context_summary": "PO-2024-0035 的数据...",
                "entities": {"po_number": "PO-2024-0035", "supplier_id": "SUP-001"},
            },
        ), patch.object(
            orch._intent_router, "route",
            return_value=_make_l3_signal("分析采购订单 PO-2024-0035的风险"),
        ), patch(
            "core.orchestrator.dag.validator.DAGValidator"
        ) as MockValidator:
            MockValidator.return_value.validate.return_value = (True, None)
            request = AnalysisRequest(
                query="分析这个po的风险", session_id="s1"
            )
            result = await orch.analyze(request)

        # 应走 DAG 路径（PO 综合风险 DAG）
        assert result.summary.get("route_type") == "DAG"
        mock_executor.execute.assert_called_once()
        # DAG 任务中应包含 PO 号
        dag_tasks = mock_executor.execute.call_args.args[0]
        po_inputs = [
            t["inputs"].get("po_number") for t in dag_tasks
            if t["inputs"].get("po_number")
        ]
        assert "PO-2024-0035" in po_inputs


class TestOrchestratorDAGShortTermMemory:
    """DAG 路径短期记忆写入测试。"""

    @pytest.mark.asyncio
    async def test_dag_saves_to_short_term_memory(self, settings: Settings) -> None:
        """DAG 执行后应通过 agent.update_state 写入 checkpointer。"""
        orch = Orchestrator(settings=settings)

        mock_agent_graph = MagicMock()
        mock_agent_graph.update_state = MagicMock()
        mock_agent = MagicMock()
        mock_agent._get_or_build_agent.return_value = mock_agent_graph
        orch._agent = mock_agent

        await orch._save_dag_to_short_term_memory(
            query="分析三路匹配",
            response="# 报告内容",
            session_id="test-session",
            time_range_days=30,
        )

        mock_agent_graph.update_state.assert_called_once()
        call_args = mock_agent_graph.update_state.call_args
        assert call_args[0][0]["configurable"]["thread_id"] == "test-session"
        messages = call_args[0][1]["messages"]
        assert len(messages) == 2

    @pytest.mark.asyncio
    async def test_dag_short_term_memory_failure_not_blocking(self, settings: Settings) -> None:
        """agent 不可用时不应阻塞主流程。"""
        orch = Orchestrator(settings=settings)
        mock_agent = MagicMock()
        mock_agent._get_or_build_agent.return_value = None
        orch._agent = mock_agent

        # 不应抛异常
        await orch._save_dag_to_short_term_memory(
            query="测试",
            response="报告",
            session_id="s1",
            time_range_days=30,
        )


class TestOrchestratorDAGPath:
    """DAG 路径测试（Level 1/2 命中）。"""

    @pytest.mark.asyncio
    async def test_dag_execution_success(self, settings: Settings) -> None:
        """Level 1 命中应走 DAG 路径。"""
        orch = Orchestrator(settings=settings)

        # Mock DAG executor
        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value={
            "status": "ok",
            "outputs": {"report": "# DAG Report"},
            "completed_tasks": ["t1", "t2", "t3"],
            "failed_tasks": {},
            "report": "# DAG Report",
            "duration_sec": 1.5,
        })
        mock_executor._registry = MagicMock()
        orch._dag_executor = mock_executor

        # L1 signal
        signal = QuerySignal(
            raw_query="分析三路匹配异常",
            keywords=[AnalysisType.THREE_WAY_MATCH.value],
            entities={},
            route_level=1,
            confidence=0.3,
            reasoning="L1 test",
        )
        with patch.object(orch._intent_router, "route", return_value=signal), \
             patch("core.orchestrator.dag.validator.DAGValidator") as MockValidator:
            MockValidator.return_value.validate.return_value = (True, None)
            request = AnalysisRequest(query="分析三路匹配异常")
            result = await orch.analyze(request)

        assert result.status == AnalysisStatus.SUCCESS
        assert result.report_markdown == "# DAG Report"
        assert result.summary.get("route_type") == "DAG"
        mock_executor.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_dag_partial_success(self, settings: Settings) -> None:
        """DAG 部分失败应返回 PARTIAL_SUCCESS。"""
        orch = Orchestrator(settings=settings)

        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value={
            "status": "warning",
            "outputs": {},
            "completed_tasks": ["t1"],
            "failed_tasks": {"t2": "timeout"},
            "report": "",
            "duration_sec": 2.0,
        })
        mock_executor._registry = MagicMock()
        orch._dag_executor = mock_executor

        signal = QuerySignal(
            raw_query="分析价格差异",
            keywords=[AnalysisType.PRICE_VARIANCE.value],
            entities={},
            route_level=1,
            confidence=0.3,
            reasoning="L1 test",
        )
        with patch.object(orch._intent_router, "route", return_value=signal), \
             patch("core.orchestrator.dag.validator.DAGValidator") as MockValidator:
            MockValidator.return_value.validate.return_value = (True, None)
            request = AnalysisRequest(query="分析价格差异")
            result = await orch.analyze(request)

        assert result.status == AnalysisStatus.PARTIAL_SUCCESS

    @pytest.mark.asyncio
    async def test_comprehensive_skips_dag(self, settings: Settings) -> None:
        """COMPREHENSIVE 类型即使 L1 命中也走 ReAct。"""
        orch = Orchestrator(settings=settings)
        orch._agent = _make_mock_agent(return_value={
            "anomalies": [], "summary": {}, "report_markdown": "react",
            "completed_tasks": [], "failed_tasks": [],
        })

        signal = QuerySignal(
            raw_query="全面分析",
            keywords=[AnalysisType.COMPREHENSIVE.value],
            entities={},
            route_level=1,
            confidence=0.3,
            reasoning="L1 test",
        )
        with patch.object(orch._intent_router, "route", return_value=signal):
            request = AnalysisRequest(query="全面分析")
            result = await orch.analyze(request)

        assert result.report_markdown == "react"


# ============================================================
# DAG 长期记忆读取 / 写入
# ============================================================


# ============================================================
# 短期记忆截断
# ============================================================


class TestCheckpointerTruncation:
    """checkpointer 历史消息截断测试。"""

    def test_truncate_no_checkpointer(self, settings: Settings) -> None:
        """无 checkpointer 时静默跳过。"""
        from modules.p2p.agent import P2PAgent
        agent = P2PAgent(settings=settings)
        agent._checkpointer = None
        # 不应抛异常
        agent._truncate_checkpointer_history("s1", 20)

    def test_truncate_under_limit(self, settings: Settings) -> None:
        """消息数未超限时不截断。"""
        from modules.p2p.agent import P2PAgent
        from langchain_core.messages import HumanMessage, AIMessage

        agent = P2PAgent(settings=settings)
        mock_cp = MagicMock()
        mock_tuple = MagicMock()
        mock_tuple.checkpoint = {
            "channel_values": {"messages": [HumanMessage(content="q"), AIMessage(content="a")]},
        }
        mock_tuple.metadata = {"step": 1}
        mock_cp.get_tuple.return_value = mock_tuple
        agent._checkpointer = mock_cp

        agent._truncate_checkpointer_history("s1", 20)
        mock_cp.put.assert_not_called()  # 未超限，不写回

    def test_truncate_over_limit(self, settings: Settings) -> None:
        """消息数超限时应截断到最近 N 条。"""
        from modules.p2p.agent import P2PAgent
        from langchain_core.messages import HumanMessage, AIMessage

        agent = P2PAgent(settings=settings)
        mock_cp = MagicMock()

        # 30 条消息，限制 10
        messages = [HumanMessage(content=f"q{i}") if i % 2 == 0 else AIMessage(content=f"a{i}") for i in range(30)]
        mock_tuple = MagicMock()
        mock_tuple.checkpoint = {
            "id": "old",
            "channel_values": {"messages": messages},
        }
        mock_tuple.metadata = {"step": 5}
        mock_cp.get_tuple.return_value = mock_tuple
        agent._checkpointer = mock_cp

        agent._truncate_checkpointer_history("s1", 10)

        mock_cp.put.assert_called_once()
        call_args = mock_cp.put.call_args
        written_checkpoint = call_args[0][1]
        written_messages = written_checkpoint["channel_values"]["messages"]
        assert len(written_messages) == 10
        # 应保留最后 10 条
        assert written_messages[0].content == "q20"

    def test_truncate_failure_non_blocking(self, settings: Settings) -> None:
        """截断失败不应抛异常。"""
        from modules.p2p.agent import P2PAgent

        agent = P2PAgent(settings=settings)
        mock_cp = MagicMock()
        mock_cp.get_tuple.side_effect = RuntimeError("db error")
        agent._checkpointer = mock_cp

        # 不应抛异常
        agent._truncate_checkpointer_history("s1", 10)


# ============================================================
# _estimate_checkpointer_tokens
# ============================================================


class TestEstimateCheckpointerTokens:
    """checkpointer 历史消息 token 估算测试。"""

    def test_no_checkpointer(self, settings: Settings) -> None:
        """无 checkpointer 时返回 (0, 0)。"""
        from modules.p2p.agent import P2PAgent
        agent = P2PAgent(settings=settings)
        agent._checkpointer = None
        tokens, count = agent._estimate_checkpointer_tokens("s1")
        assert tokens == 0
        assert count == 0

    def test_empty_history(self, settings: Settings) -> None:
        """空历史返回 (0, 0)。"""
        from modules.p2p.agent import P2PAgent
        agent = P2PAgent(settings=settings)
        mock_cp = MagicMock()
        mock_cp.get_tuple.return_value = None
        agent._checkpointer = mock_cp
        tokens, count = agent._estimate_checkpointer_tokens("s1")
        assert tokens == 0
        assert count == 0

    def test_with_messages(self, settings: Settings) -> None:
        """有历史消息时应返回正整数。"""
        from langchain_core.messages import AIMessage, HumanMessage
        from modules.p2p.agent import P2PAgent

        agent = P2PAgent(settings=settings)
        mock_cp = MagicMock()
        mock_tuple = MagicMock()
        mock_tuple.checkpoint = {
            "channel_values": {
                "messages": [
                    HumanMessage(content="查询 SUP-001 的绩效"),
                    AIMessage(content="SUP-001 准时交付率 92%，详细报告如下..." * 10),
                ]
            }
        }
        mock_cp.get_tuple.return_value = mock_tuple
        agent._checkpointer = mock_cp

        tokens, count = agent._estimate_checkpointer_tokens("s1")
        assert tokens > 0
        assert count == 2

    def test_failure_returns_zero(self, settings: Settings) -> None:
        """异常时返回 (0, 0)，不抛异常。"""
        from modules.p2p.agent import P2PAgent
        agent = P2PAgent(settings=settings)
        mock_cp = MagicMock()
        mock_cp.get_tuple.side_effect = RuntimeError("db error")
        agent._checkpointer = mock_cp
        tokens, count = agent._estimate_checkpointer_tokens("s1")
        assert tokens == 0
        assert count == 0


# ============================================================
# _estimate_tool_definitions_tokens
# ============================================================


class TestEstimateToolDefinitionsTokens:
    """工具定义 schema token 估算测试。"""

    def test_returns_positive(self, settings: Settings) -> None:
        """8 个工具的 schema 应有正数 token。"""
        from modules.p2p.agent import P2PAgent
        agent = P2PAgent(settings=settings)
        tokens = agent._estimate_tool_definitions_tokens()
        assert tokens > 0
        assert isinstance(tokens, int)

    def test_cached(self, settings: Settings) -> None:
        """第二次调用应命中缓存，结果相同。"""
        from modules.p2p.agent import P2PAgent
        agent = P2PAgent(settings=settings)
        t1 = agent._estimate_tool_definitions_tokens()
        t2 = agent._estimate_tool_definitions_tokens()
        assert t1 == t2
        assert hasattr(agent, "_tool_definitions_tokens_cache")


# ============================================================
# DAG context_budget 记录
# ============================================================


class TestDAGContextBudget:
    """DAG 路径 context_budget span 测试。"""

    def test_record_dag_context_budget_emits_span(self, settings: Settings) -> None:
        """_record_dag_context_budget 应在活跃 trace 中记录 context_budget span。"""
        from core.observability.middleware import TimingMiddleware, _current_trace

        orch = Orchestrator(settings=settings)
        mw = orch._timing_middleware
        mw._print = False
        mw.start_run()

        orch._record_dag_context_budget(query="分析价格差异")

        ctx = _current_trace.get()
        budget_spans = [s for s in ctx.spans if s.span_type == "context_budget"]
        assert len(budget_spans) == 1
        attrs = budget_spans[0].attributes
        assert attrs["route_type"] == "DAG"
        assert attrs["long_term_memory_tokens"] == 0
        assert attrs["user_message_tokens"] > 0
        assert attrs["model_context_limit"] == settings.llm.context_window
        assert 0 <= attrs["budget_usage_pct"] <= 100

        mw.finish_run()


# ============================================================
# trim_to_token_budget
# ============================================================


class TestTrimToTokenBudget:
    """trim_to_token_budget 集中裁剪函数测试。"""

    def test_within_budget_no_trim(self) -> None:
        """未超预算时原样返回。"""
        from modules.p2p.prompts import trim_to_token_budget
        text = "短文本"
        assert trim_to_token_budget(text, 1000, "测试") == text

    def test_exceeds_budget_trimmed(self) -> None:
        """超出预算时应裁剪，返回更短的文本。"""
        from modules.p2p.prompts import trim_to_token_budget
        long_text = "这是一段非常长的记忆内容" * 500  # ~5000 字符
        result = trim_to_token_budget(long_text, 100, "测试")
        assert len(result) < len(long_text)
        assert len(result) > 0

    def test_empty_text(self) -> None:
        """空文本应原样返回。"""
        from modules.p2p.prompts import trim_to_token_budget
        assert trim_to_token_budget("", 100, "测试") == ""
        assert trim_to_token_budget(None, 100, "测试") is None

    def test_zero_budget(self) -> None:
        """预算为 0 时应原样返回（不做裁剪）。"""
        from modules.p2p.prompts import trim_to_token_budget
        text = "一些文本"
        assert trim_to_token_budget(text, 0, "测试") == text


# ============================================================
# format_long_term_memory 集成裁剪
# ============================================================


class TestFormatLongTermMemoryTrim:
    """format_long_term_memory 中的集中裁剪测试。"""

    def test_normal_records_within_budget(self) -> None:
        """正常长度的记录应不被裁剪。"""
        from modules.p2p.prompts import format_long_term_memory
        records = [
            {"content": "分析结论：SUP-001 准时率 92%"},
            {"content": "分析结论：价格差异 3 笔"},
        ]
        result = format_long_term_memory(records)
        assert "SUP-001" in result
        assert "价格差异" in result

    def test_very_long_records_trimmed(self) -> None:
        """超长记录拼接后应被裁剪到预算内。"""
        from modules.p2p.prompts import format_long_term_memory
        from core.observability.middleware import estimate_tokens
        from config.settings import get_settings

        settings = get_settings()
        max_tokens = int(
            settings.llm.context_window
            * settings.memory.long_term_context_max_tokens_pct
            / 100
        )

        # 构造超出预算的大量记录（每条 300 字符 × 50 条 = 15000 字符）
        records = [{"content": f"分析{i}: " + "详细内容" * 40} for i in range(50)]
        result = format_long_term_memory(records)

        result_tokens = estimate_tokens(result)
        assert result_tokens <= max_tokens + 1  # 允许 1 token 误差

    def test_trim_disabled_no_cut(self) -> None:
        """关闭裁剪开关时不应裁剪。"""
        from modules.p2p.prompts import format_long_term_memory

        records = [{"content": "分析: " + "长内容" * 50} for _ in range(50)]

        mock_settings = MagicMock()
        mock_settings.memory.long_term_context_trim_enabled = False

        with patch(
            "config.settings.get_settings", return_value=mock_settings
        ):
            result = format_long_term_memory(records)

        # 关闭裁剪时应保留完整文本
        assert len(result) > 0


# ============================================================
# _load_session_context 短期记忆裁剪
# ============================================================


class TestSessionContextTrim:
    """_load_session_context 中短期记忆集中裁剪测试。"""

    def test_short_context_no_trim(self, settings: Settings) -> None:
        """短摘要不应被裁剪。"""
        from langchain_core.messages import AIMessage, HumanMessage

        orch = Orchestrator(settings=settings)
        mock_cp = MagicMock()
        mock_tuple = MagicMock()
        mock_tuple.checkpoint = {
            "channel_values": {
                "messages": [
                    HumanMessage(content="查询 SUP-001"),
                    AIMessage(content="SUP-001 准时率 95%"),
                ]
            }
        }
        mock_cp.get_tuple.return_value = mock_tuple
        orch._checkpointer = mock_cp

        ctx = orch._load_session_context("s1")
        assert ctx["context_summary"] == "SUP-001 准时率 95%"

    def test_very_long_context_trimmed(self, settings: Settings) -> None:
        """超长 AI 回复应被裁剪到 token 预算内。"""
        from langchain_core.messages import AIMessage, HumanMessage
        from core.observability.middleware import estimate_tokens

        orch = Orchestrator(settings=settings)
        mock_cp = MagicMock()
        # 构造超长 AI 回复（~30000 字符 >> 15% of 32768 tokens）
        long_response = "分析报告内容详情" * 3000
        mock_tuple = MagicMock()
        mock_tuple.checkpoint = {
            "channel_values": {
                "messages": [
                    HumanMessage(content="分析所有数据"),
                    AIMessage(content=long_response),
                ]
            }
        }
        mock_cp.get_tuple.return_value = mock_tuple
        orch._checkpointer = mock_cp

        ctx = orch._load_session_context("s1")
        summary = ctx["context_summary"]

        max_tokens = int(
            settings.llm.context_window
            * settings.memory.short_term_context_max_tokens_pct
            / 100
        )
        assert estimate_tokens(summary) <= max_tokens + 1
        assert len(summary) < len(long_response)


# ============================================================
# _resolve_time_range
# ============================================================


class TestResolveTimeRange:
    """时间范围解析测试。"""

    def test_none_returns_none(self) -> None:
        from core.orchestrator.orchestrator import _resolve_time_range

        assert _resolve_time_range(None) is None

    def test_empty_returns_none(self) -> None:
        from core.orchestrator.orchestrator import _resolve_time_range

        assert _resolve_time_range("") is None

    def test_nd_format(self) -> None:
        from core.orchestrator.orchestrator import _resolve_time_range

        assert _resolve_time_range("7d") == 7
        assert _resolve_time_range("30d") == 30
        assert _resolve_time_range("365d") == 365

    def test_this_month(self) -> None:
        from core.orchestrator.orchestrator import _resolve_time_range

        result = _resolve_time_range("this_month")
        assert result is not None
        assert 1 <= result <= 31

    def test_last_month(self) -> None:
        from core.orchestrator.orchestrator import _resolve_time_range

        result = _resolve_time_range("last_month")
        assert result is not None
        assert 28 <= result <= 62

    def test_invalid_returns_none(self) -> None:
        from core.orchestrator.orchestrator import _resolve_time_range

        assert _resolve_time_range("abc") is None
        assert _resolve_time_range("7days") is None


# ============================================================
# Orchestrator 超时测试
# ============================================================


class TestOrchestratorTimeout:
    """整体超时测试。"""

    @pytest.mark.asyncio
    async def test_analyze_timeout_returns_failed(self, settings: Settings) -> None:
        """Agent 执行超时应返回 RESPONSE_TIMEOUT。"""
        import asyncio

        settings.analysis.response_timeout_seconds = 0.1  # 100ms

        orch = Orchestrator(settings=settings)

        async def slow_inner(**kwargs):
            await asyncio.sleep(5)

        with patch.object(orch, "_analyze_inner", side_effect=slow_inner):
            request = AnalysisRequest(query="超时测试")
            result = await orch.analyze(request)

        assert result.status == AnalysisStatus.FAILED
        assert result.error is not None
        assert result.error.code == "RESPONSE_TIMEOUT"


# ============================================================
# Orchestrator checkpointer 生命周期
# ============================================================


class TestCheckpointerLifecycle:
    """Checkpointer 初始化与关闭测试。"""

    def test_ensure_checkpointer_failure_returns_none(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        with patch.object(orch, "_get_checkpointer", side_effect=RuntimeError("pg down")):
            result = orch._ensure_checkpointer()
        assert result is None

    def test_close_checkpointer_exception(self, settings: Settings) -> None:
        """关闭 checkpointer 异常不应阻塞。"""
        orch = Orchestrator(settings=settings)
        mock_cm = MagicMock()
        mock_cm.__exit__ = MagicMock(side_effect=RuntimeError("close error"))
        orch._checkpointer_cm = mock_cm
        orch._checkpointer = MagicMock()

        orch._close_checkpointer()
        assert orch._checkpointer is None
        assert orch._checkpointer_cm is None

    def test_close_checkpointer_idempotent(self, settings: Settings) -> None:
        """重复关闭不应报错。"""
        orch = Orchestrator(settings=settings)
        orch._close_checkpointer()  # cm 为 None，应静默返回


# ============================================================
# Orchestrator _persist_report 测试
# ============================================================


class TestPersistReport:
    def test_persist_report_failure_logged(self, settings: Settings) -> None:
        """持久化失败不应抛异常。"""
        from api.schemas.analysis import AnalysisResult, AnalysisStatus, AnalysisType

        orch = Orchestrator(settings=settings)
        result = AnalysisResult(
            report_id="r1", status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query="测试", user_id="u1", session_id="s1",
            time_range="30d",
        )
        with patch("core.memory.get_long_term_memory", side_effect=RuntimeError("db")):
            orch._persist_report(result)  # 不应抛异常


# ============================================================
# _validate_entities
# ============================================================


class TestValidateEntities:
    """实体 DB 验证测试。"""

    @pytest.mark.asyncio
    async def test_po_not_found_discarded(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        mock_repo = MagicMock()
        mock_repo.query_purchase_orders.return_value = []
        params = {"po_number": "PO-9999", "days": 30}

        await orch._validate_entities(mock_repo, params)
        assert "po_number" not in params

    @pytest.mark.asyncio
    async def test_po_found_kept(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        mock_repo = MagicMock()
        mock_repo.query_purchase_orders.return_value = [{"po_number": "PO-001"}]
        params = {"po_number": "PO-001", "days": 30}

        await orch._validate_entities(mock_repo, params)
        assert params["po_number"] == "PO-001"

    @pytest.mark.asyncio
    async def test_supplier_not_found_discarded(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        mock_repo = MagicMock()
        mock_repo.query_purchase_orders.return_value = []
        params = {"supplier_id": "SUP-999", "days": 30}

        await orch._validate_entities(mock_repo, params)
        assert "supplier_id" not in params

    @pytest.mark.asyncio
    async def test_invoice_not_found_discarded(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        mock_repo = MagicMock()
        mock_repo.query_invoices.return_value = [{"invoice_number": "INV-OTHER"}]
        params = {"invoice_number": "INV-999", "days": 30}

        await orch._validate_entities(mock_repo, params)
        assert "invoice_number" not in params

    @pytest.mark.asyncio
    async def test_payment_not_found_discarded(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        mock_repo = MagicMock()
        mock_repo.query_payments.return_value = []
        params = {"payment_number": "PAY-999", "days": 30}

        await orch._validate_entities(mock_repo, params)
        assert "payment_number" not in params

    @pytest.mark.asyncio
    async def test_receipt_not_found_discarded(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        mock_repo = MagicMock()
        mock_repo.query_receipts.return_value = [{"receipt_id": "RCV-OTHER"}]
        params = {"receipt_number": "RCV-999", "days": 30}

        await orch._validate_entities(mock_repo, params)
        assert "receipt_number" not in params

    @pytest.mark.asyncio
    async def test_db_error_non_blocking(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        mock_repo = MagicMock()
        mock_repo.query_purchase_orders.side_effect = RuntimeError("db error")
        params = {"po_number": "PO-001", "days": 30}

        await orch._validate_entities(mock_repo, params)
        assert params["po_number"] == "PO-001"  # 验证失败保留实体


# ============================================================
# _enrich_entities
# ============================================================


class TestEnrichEntities:
    """实体关联补充测试。"""

    @pytest.mark.asyncio
    async def test_payment_to_invoice(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        mock_repo = MagicMock()
        mock_repo.query_payments.return_value = [{"invoice_number": "INV-001"}]
        mock_repo.query_purchase_orders.return_value = []
        mock_repo.query_invoices.return_value = []
        mock_repo.query_receipts.return_value = []

        params = {"payment_number": "PAY-001", "days": 30}

        with patch("modules.p2p.tools._get_repository", return_value=mock_repo):
            await orch._enrich_entities(params)

        assert params.get("invoice_number") == "INV-001"

    @pytest.mark.asyncio
    async def test_po_to_supplier(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        mock_repo = MagicMock()
        mock_repo.query_purchase_orders.return_value = [{"supplier_id": "SUP-001"}]
        mock_repo.query_invoices.return_value = []
        mock_repo.query_receipts.return_value = []
        mock_repo.query_payments.return_value = []

        params = {"po_number": "PO-001", "days": 30}

        with patch("modules.p2p.tools._get_repository", return_value=mock_repo):
            await orch._enrich_entities(params)

        assert params.get("supplier_id") == "SUP-001"

    @pytest.mark.asyncio
    async def test_receipt_to_po_and_supplier(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        mock_repo = MagicMock()
        mock_repo.query_receipts.return_value = [
            {"receipt_id": "RCV-001", "po_number": "PO-001", "supplier_id": "SUP-001"}
        ]
        mock_repo.query_purchase_orders.return_value = []
        mock_repo.query_invoices.return_value = []
        mock_repo.query_payments.return_value = []

        params = {"receipt_number": "RCV-001", "days": 30}

        with patch("modules.p2p.tools._get_repository", return_value=mock_repo):
            await orch._enrich_entities(params)

        assert params.get("po_number") == "PO-001"
        assert params.get("supplier_id") == "SUP-001"

    @pytest.mark.asyncio
    async def test_invoice_to_po_and_supplier(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings)
        mock_repo = MagicMock()
        mock_repo.query_invoices.return_value = [
            {"invoice_number": "INV-001", "po_number": "PO-002", "supplier_id": "SUP-002"}
        ]
        mock_repo.query_purchase_orders.return_value = []
        mock_repo.query_receipts.return_value = []
        mock_repo.query_payments.return_value = []

        params = {"invoice_number": "INV-001", "days": 30}

        with patch("modules.p2p.tools._get_repository", return_value=mock_repo):
            await orch._enrich_entities(params)

        assert params.get("po_number") == "PO-002"
        assert params.get("supplier_id") == "SUP-002"

    @pytest.mark.asyncio
    async def test_repo_init_failure(self, settings: Settings) -> None:
        """_get_repository 失败不阻塞。"""
        orch = Orchestrator(settings=settings)
        params = {"po_number": "PO-001", "days": 30}

        with patch("modules.p2p.tools._get_repository", side_effect=RuntimeError("no repo")):
            await orch._enrich_entities(params)

        assert params["po_number"] == "PO-001"  # 原样保留
