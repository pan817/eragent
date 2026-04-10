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
        """Level 3 走 ReAct 路径应成功。"""
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
