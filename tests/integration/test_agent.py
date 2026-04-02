"""P2P Agent 和 Orchestrator 集成测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from api.schemas.analysis import AnalysisRequest, AnalysisStatus, AnalysisType
from config.settings import Settings


class TestP2PAgentInit:
    """P2P Agent 初始化测试。"""

    def test_init_with_settings(self) -> None:
        """传入 settings 时不应调用 get_settings。"""
        with patch("modules.p2p.agent.get_settings") as mock_get:
            from modules.p2p.agent import P2PAgent
            settings = Settings()
            agent = P2PAgent(settings=settings)
            mock_get.assert_not_called()
            assert agent._settings is settings

    def test_init_without_settings(self) -> None:
        """不传 settings 时应自动调用 get_settings。"""
        mock_settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=mock_settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent()
            assert agent._settings is mock_settings


class TestP2PAgentAnalyze:
    """P2P Agent 分析执行测试（mock LLM）。"""

    def test_analyze_returns_result(self) -> None:
        """analyze 应返回 AnalysisResult。"""
        with patch("modules.p2p.agent.get_settings", return_value=Settings()):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent()

        mock_agent = MagicMock()
        mock_message = MagicMock()
        mock_message.content = "# 测试分析报告\n\n无异常发现"
        mock_agent.invoke.return_value = {"messages": [mock_message]}
        agent._agent = mock_agent

        result = agent.analyze("分析三路匹配", user_id="test-user")
        assert result.status == AnalysisStatus.SUCCESS
        assert "测试分析报告" in result.report_markdown

    def test_analyze_failure_returns_failed(self) -> None:
        """LLM 调用失败时应返回 FAILED 状态。"""
        settings = Settings()
        settings.llm.max_retries = 1
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_agent = MagicMock()
        mock_agent.invoke.side_effect = RuntimeError("API 连接失败")
        agent._agent = mock_agent

        result = agent.analyze("测试查询")
        assert result.status == AnalysisStatus.FAILED
        assert result.error is not None
        assert "失败" in result.error.message
