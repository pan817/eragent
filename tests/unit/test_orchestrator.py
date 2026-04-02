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


def _make_mock_agent(**run_kwargs: object) -> MagicMock:
    """创建带 AsyncMock run 方法的 mock agent。"""
    agent = MagicMock()
    agent.run = AsyncMock(**run_kwargs)
    return agent


@pytest.fixture()
def settings() -> Settings:
    return Settings()


class TestOrchestratorInit:
    """初始化测试。"""

    def test_init_with_settings(self, settings: Settings) -> None:
        """传入 settings 时应使用传入值。"""
        orch = Orchestrator(settings=settings)
        assert orch._settings is settings

    def test_init_without_settings(self) -> None:
        """不传 settings 时应自动获取。"""
        mock_settings = Settings()
        with patch("core.orchestrator.orchestrator.get_settings", return_value=mock_settings):
            orch = Orchestrator()
        assert orch._settings is mock_settings

    def test_lazy_agent_property(self, settings: Settings) -> None:
        """_lazy_agent 首次访问时应创建 P2PAgent。"""
        orch = Orchestrator(settings=settings)
        assert orch._agent is None

        mock_p2p = MagicMock()
        with patch("modules.p2p.agent.P2PAgent", return_value=mock_p2p):
            agent = orch._lazy_agent
        assert agent is mock_p2p
        # 第二次访问应复用
        assert orch._lazy_agent is mock_p2p


class TestOrchestratorAnalyze:
    """分析流程测试。"""

    @pytest.mark.asyncio
    async def test_analyze_success(self, settings: Settings) -> None:
        """成功分析应返回 SUCCESS 状态。"""
        orch = Orchestrator(settings=settings)

        orch._agent = _make_mock_agent(return_value={
            "anomalies": [],
            "supplier_kpis": [],
            "summary": {"total": 0},
            "report_markdown": "# Report",
            "completed_tasks": ["three_way_match"],
            "failed_tasks": [],
        })

        request = AnalysisRequest(query="分析三路匹配异常")
        result = await orch.analyze(request)

        assert result.status == AnalysisStatus.SUCCESS
        assert result.report_id  # 应有 report_id
        assert result.report_markdown == "# Report"

    @pytest.mark.asyncio
    async def test_analyze_with_explicit_type(self, settings: Settings) -> None:
        """显式指定 analysis_type 时应优先使用。"""
        orch = Orchestrator(settings=settings)

        orch._agent = _make_mock_agent(return_value={
            "anomalies": [],
            "summary": {},
            "report_markdown": "",
            "completed_tasks": [],
            "failed_tasks": [],
        })

        request = AnalysisRequest(
            query="分析价格",
            analysis_type=AnalysisType.PRICE_VARIANCE,
        )
        result = await orch.analyze(request)
        assert result.analysis_type == AnalysisType.PRICE_VARIANCE

    @pytest.mark.asyncio
    async def test_analyze_with_time_range(self, settings: Settings) -> None:
        """指定 time_range_days 应传递给 Agent。"""
        orch = Orchestrator(settings=settings)

        orch._agent = _make_mock_agent(return_value={
            "anomalies": [],
            "summary": {},
            "report_markdown": "",
            "completed_tasks": [],
            "failed_tasks": [],
        })

        request = AnalysisRequest(query="分析", time_range_days=60)
        result = await orch.analyze(request)
        assert "60" in result.time_range

    @pytest.mark.asyncio
    async def test_analyze_failure(self, settings: Settings) -> None:
        """Agent 异常时应返回 FAILED 状态。"""
        orch = Orchestrator(settings=settings)

        orch._agent = _make_mock_agent(side_effect=RuntimeError("boom"))

        request = AnalysisRequest(query="测试")
        result = await orch.analyze(request)

        assert result.status == AnalysisStatus.FAILED
        assert result.error is not None
        assert "boom" in result.error.message

    @pytest.mark.asyncio
    async def test_analyze_with_session_id(self, settings: Settings) -> None:
        """请求中的 session_id 应被使用。"""
        orch = Orchestrator(settings=settings)

        orch._agent = _make_mock_agent(return_value={
            "anomalies": [],
            "summary": {},
            "report_markdown": "",
            "completed_tasks": [],
            "failed_tasks": [],
        })

        request = AnalysisRequest(query="分析", session_id="my-session")
        result = await orch.analyze(request)
        assert result.session_id == "my-session"
