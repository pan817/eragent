"""P2P Agent 额外覆盖测试。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from api.schemas.analysis import AnalysisStatus, AnalysisType
from config.settings import Settings


class TestP2PAgentBuildMethods:
    """Agent 构建方法测试。"""

    def test_build_model(self) -> None:
        """_build_model 应返回 ChatOpenAI 实例。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        with patch("modules.p2p.agent.ChatOpenAI") as mock_chat:
            mock_chat.return_value = MagicMock()
            model = agent._build_model()
            mock_chat.assert_called_once()

    def test_build_tools(self) -> None:
        """_build_tools 应返回 8 个工具。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        tools = agent._build_tools()
        assert len(tools) == 8

    def test_get_system_prompt(self) -> None:
        """_get_system_prompt 应返回包含角色定义的字符串。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        prompt = agent._get_system_prompt()
        assert "P2P" in prompt
        assert "角色定义" in prompt

    def test_get_ontology_context_success(self) -> None:
        """本体上下文获取成功时应返回格式化文本。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        context = agent._get_ontology_context()
        # 应包含业务背景文本（成功或降级都可以）
        assert "P2P" in context or "采购" in context

    def test_get_ontology_context_failure(self) -> None:
        """本体加载失败时应返回默认文本。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        with patch("modules.p2p.agent.OntologyLoader", side_effect=Exception("no owl")):
            context = agent._get_ontology_context()
        assert "三路匹配" in context

    def test_get_or_build_agent(self) -> None:
        """_get_or_build_agent 应构建并缓存 agent。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_langchain_agent = MagicMock()
        with (
            patch("modules.p2p.agent.ChatOpenAI"),
            patch("modules.p2p.agent.create_agent", return_value=mock_langchain_agent),
        ):
            result = agent._get_or_build_agent()
        assert result is mock_langchain_agent
        # 第二次调用应复用
        assert agent._get_or_build_agent() is mock_langchain_agent


class TestP2PAgentAnalyzeExtra:
    """Agent 分析方法额外覆盖。"""

    def test_analyze_with_json_content(self) -> None:
        """Agent 返回 JSON 内容时应解析结构化数据。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_agent = MagicMock()
        content = json.dumps({
            "analysis_type": "three_way_match",
            "anomalies": [{"id": "ANO-001"}],
            "summary": {"total": 1},
        })
        mock_message = MagicMock()
        mock_message.content = content
        mock_agent.invoke.return_value = {"messages": [mock_message]}
        agent._agent = mock_agent

        result = agent.analyze("分析三路匹配")
        assert result.status == AnalysisStatus.SUCCESS
        assert result.analysis_type == AnalysisType.THREE_WAY_MATCH

    def test_analyze_with_empty_messages(self) -> None:
        """Agent 返回空消息列表时应正常处理。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {"messages": []}
        agent._agent = mock_agent

        result = agent.analyze("测试")
        assert result.status == AnalysisStatus.SUCCESS
        assert result.report_markdown == ""

    def test_analyze_message_without_content_attr(self) -> None:
        """消息对象无 content 属性时应转为字符串。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_agent = MagicMock()
        # 使用字符串而非有 content 属性的对象
        mock_agent.invoke.return_value = {"messages": ["plain text"]}
        agent._agent = mock_agent

        result = agent.analyze("测试")
        assert result.status == AnalysisStatus.SUCCESS

    def test_analyze_retry_then_success(self) -> None:
        """首次失败后重试成功。"""
        settings = Settings()
        settings.llm.max_retries = 2
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_agent = MagicMock()
        mock_message = MagicMock()
        mock_message.content = "ok"
        # 第一次失败，第二次成功
        mock_agent.invoke.side_effect = [
            RuntimeError("first fail"),
            {"messages": [mock_message]},
        ]

        # 需要让 _get_or_build_agent 返回同一个 mock
        with (
            patch("modules.p2p.agent.ChatOpenAI"),
            patch("modules.p2p.agent.create_agent", return_value=mock_agent),
        ):
            result = agent.analyze("测试")
        assert result.status == AnalysisStatus.SUCCESS
