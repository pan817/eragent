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
        """无指代词时应全部补充。"""
        ctx = {"has_history": True, "entities": {"po_number": "PO-001", "supplier_id": "SUP-001"}}
        enhanced, entities = Orchestrator._resolve_references("分析价格差异", ctx)
        assert enhanced == "分析价格差异"
        assert "po_number" in entities
        assert "supplier_id" in entities

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
        mock_agent = MagicMock()
        mock_agent._checkpointer = None
        orch._agent = mock_agent

        ctx = orch._load_session_context("s1")
        assert ctx["has_history"] is False

    def test_load_context_no_history(self, settings: Settings) -> None:
        """无历史时返回空上下文。"""
        orch = Orchestrator(settings=settings)
        mock_checkpointer = MagicMock()
        mock_checkpointer.get_tuple.return_value = None
        mock_agent = MagicMock()
        mock_agent._checkpointer = mock_checkpointer
        orch._agent = mock_agent

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
        mock_agent = MagicMock()
        mock_agent._checkpointer = mock_checkpointer
        orch._agent = mock_agent

        ctx = orch._load_session_context("s1")
        assert ctx["has_history"] is True
        assert ctx["entities"].get("po_number") is not None
        assert ctx["entities"].get("supplier_id") is not None

    def test_load_context_failure_not_blocking(self, settings: Settings) -> None:
        """checkpointer 读取异常不应阻塞。"""
        orch = Orchestrator(settings=settings)
        mock_checkpointer = MagicMock()
        mock_checkpointer.get_tuple.side_effect = RuntimeError("db error")
        mock_agent = MagicMock()
        mock_agent._checkpointer = mock_checkpointer
        orch._agent = mock_agent

        ctx = orch._load_session_context("s1")
        assert ctx["has_history"] is False

    @pytest.mark.asyncio
    async def test_context_po_entity_triggers_dag(self, settings: Settings) -> None:
        """'分析这个po' + 上下文有 PO 号 → 指代消解后走 PO 综合 DAG。"""
        orch = Orchestrator(settings=settings)

        # Mock DAG executor
        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value={
            "status": "completed",
            "outputs": {"report": "# PO Risk"},
            "completed_tasks": ["t1"],
            "failed_tasks": {},
            "report": "# PO Risk",
            "duration_sec": 1.0,
        })
        mock_executor._registry = MagicMock()
        orch._dag_executor = mock_executor

        # Mock agent (for checkpointer access)
        mock_agent = MagicMock()
        mock_agent._checkpointer = None
        orch._agent = mock_agent

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
        """DAG 执行后应将 query+report 写入 checkpointer。"""
        orch = Orchestrator(settings=settings)

        # Mock checkpointer
        mock_checkpointer = MagicMock()
        mock_checkpointer.get_tuple.return_value = None  # 无历史
        mock_checkpointer.put = MagicMock()

        mock_agent = MagicMock()
        mock_agent._checkpointer = mock_checkpointer
        orch._agent = mock_agent

        await orch._save_dag_to_short_term_memory(
            query="分析三路匹配",
            response="# 报告内容",
            session_id="test-session",
            time_range_days=30,
        )

        mock_checkpointer.put.assert_called_once()

    @pytest.mark.asyncio
    async def test_dag_short_term_memory_failure_not_blocking(self, settings: Settings) -> None:
        """checkpointer 写入失败不应阻塞主流程。"""
        orch = Orchestrator(settings=settings)

        mock_agent = MagicMock()
        mock_agent._checkpointer = None  # checkpointer 不可用
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
            "status": "completed",
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
            "status": "partial",
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
