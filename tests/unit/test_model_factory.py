"""P2P model_factory 及 Agent 内部方法单元测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.schemas.analysis import AnalysisStatus, AnalysisType
from config.settings import Settings


class TestModelFactory:
    """build_chat_model 单元测试。"""

    def test_build_model(self) -> None:
        """build_chat_model 应调用 ChatOpenAI 构造器。"""
        settings = Settings()
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            mock_chat.return_value = MagicMock()
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm)
            mock_chat.assert_called_once()

    def test_build_model_default_disables_system_proxy(self) -> None:
        """默认 use_system_proxy=False：应注入 trust_env=False 的 httpx 客户端。"""
        settings = Settings()
        assert settings.llm.use_system_proxy is False
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm)
            kwargs = mock_chat.call_args.kwargs
            # 全程走 async 路径，仅注入 AsyncClient，避免空闲的同步连接池
            assert "http_client" not in kwargs
            assert "http_async_client" in kwargs
            assert kwargs["http_async_client"]._trust_env is False

    def test_build_model_use_system_proxy(self) -> None:
        """use_system_proxy=True：不应注入自定义 httpx 客户端，沿用默认行为。"""
        settings = Settings()
        settings.llm.use_system_proxy = True
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm)
            kwargs = mock_chat.call_args.kwargs
            assert "http_client" not in kwargs
            assert "http_async_client" not in kwargs

    def test_disable_thinking_qwen3(self) -> None:
        """disable_thinking=True + qwen3 模型：应注入 extra_body。"""
        settings = Settings()
        settings.llm.provider = "qwen"
        settings.llm.model = "qwen3-max"
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm, disable_thinking=True)
            kwargs = mock_chat.call_args.kwargs
            assert kwargs["extra_body"] == {
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }

    def test_disable_thinking_qwen_non_qwen3(self) -> None:
        """disable_thinking=True + 非 qwen3 模型：不应注入 extra_body。"""
        settings = Settings()
        settings.llm.provider = "qwen"
        settings.llm.model = "qwen-plus"
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm, disable_thinking=True)
            kwargs = mock_chat.call_args.kwargs
            assert "extra_body" not in kwargs

    def test_disable_thinking_zhipu(self) -> None:
        """disable_thinking=True + zhipu：应注入 thinking.type=disabled。"""
        settings = Settings()
        settings.llm.provider = "zhipu"
        settings.llm.model = "glm-4.7"
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm, disable_thinking=True)
            kwargs = mock_chat.call_args.kwargs
            assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}

    def test_disable_thinking_minimax(self) -> None:
        """disable_thinking=True + minimax：应注入 thinking.type=disabled。"""
        settings = Settings()
        settings.llm.provider = "minimax"
        settings.llm.model = "MiniMax-M2.7"
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm, disable_thinking=True)
            kwargs = mock_chat.call_args.kwargs
            assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}

    def test_disable_thinking_unsupported_provider(self) -> None:
        """disable_thinking=True + 不支持的 provider：不应注入 extra_body。"""
        settings = Settings()
        settings.llm.provider = "deepseek"
        settings.llm.model = "deepseek-chat"
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm, disable_thinking=True)
            kwargs = mock_chat.call_args.kwargs
            assert "extra_body" not in kwargs

    def test_disable_thinking_case_insensitive_qwen(self) -> None:
        """provider / model 大小写混写也应命中 qwen3 分支。"""
        settings = Settings()
        settings.llm.provider = "QWEN"
        settings.llm.model = "Qwen3-Max"
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm, disable_thinking=True)
            kwargs = mock_chat.call_args.kwargs
            assert kwargs["extra_body"] == {
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }

    def test_disable_thinking_case_insensitive_zhipu(self) -> None:
        """provider 大小写混写也应命中 zhipu 分支。"""
        settings = Settings()
        settings.llm.provider = "ZhiPu"
        settings.llm.model = "GLM-4.6"
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm, disable_thinking=True)
            kwargs = mock_chat.call_args.kwargs
            assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}

    def test_disable_thinking_provider_prefix_zhipuai(self) -> None:
        """provider=zhipuai（子品牌前缀）应命中 zhipu 分支。"""
        settings = Settings()
        settings.llm.provider = "zhipuai"
        settings.llm.model = "glm-4.6"
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm, disable_thinking=True)
            kwargs = mock_chat.call_args.kwargs
            assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}

    def test_disable_thinking_provider_prefix_qwen_intl(self) -> None:
        """provider=qwen-intl 且 model 为 qwen3 系列应命中 qwen 分支。"""
        settings = Settings()
        settings.llm.provider = "qwen-intl"
        settings.llm.model = "qwen3-max"
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm, disable_thinking=True)
            kwargs = mock_chat.call_args.kwargs
            assert kwargs["extra_body"] == {
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }

    def test_disable_thinking_qwen_prefix_non_qwen3_still_skipped(self) -> None:
        """provider 前缀匹配 qwen 但 model 非 qwen3 系列仍应跳过（护栏不拆）。"""
        settings = Settings()
        settings.llm.provider = "qwen-intl"
        settings.llm.model = "qwen-plus"
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm, disable_thinking=True)
            kwargs = mock_chat.call_args.kwargs
            assert "extra_body" not in kwargs

    def test_disable_thinking_false_no_extra_body(self) -> None:
        """disable_thinking=False（默认）：任何 provider 均不注入 extra_body。"""
        settings = Settings()
        settings.llm.provider = "qwen"
        settings.llm.model = "qwen3-max"
        with patch("modules.p2p.model_factory.ChatOpenAI") as mock_chat:
            from modules.p2p.model_factory import build_chat_model

            build_chat_model(settings.llm, disable_thinking=False)
            kwargs = mock_chat.call_args.kwargs
            assert "extra_body" not in kwargs


class TestP2PAgentBuild:
    """P2P Agent 构建方法测试。"""

    def test_build_tools(self) -> None:
        """_build_tools 应返回 8 个工具。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        tools = agent._build_tools()
        assert len(tools) == 8

    def test_get_system_prompt(self) -> None:
        """build_system_prompt 应返回包含角色定义的字符串。"""
        from modules.p2p.prompts import build_system_prompt

        prompt = build_system_prompt()
        assert "P2P" in prompt
        assert "角色定义" in prompt

    def test_system_prompt_contains_p0_guardrails(self) -> None:
        """system prompt 应包含只读边界与数据诚信约束（P0）。"""
        from modules.p2p.prompts import build_system_prompt

        prompt = build_system_prompt()
        assert "只读" in prompt
        assert "建议人工处理" in prompt
        assert "数据诚信" in prompt
        assert ("不要虚构" in prompt) or ("不得编造" in prompt) or ("不要基于训练知识" in prompt)
        assert "执行边界" in prompt

    def test_system_prompt_contains_p1_date_and_heuristic(self) -> None:
        """system prompt 应注入日期基准且流程改为启发式（P1）。"""
        import re
        from modules.p2p.prompts import build_system_prompt

        prompt = build_system_prompt()
        # 日期 + 时区
        assert re.search(r"\d{4}-\d{2}-\d{2}", prompt)
        assert "Asia/Shanghai" in prompt
        assert "时间上下文" in prompt
        # 流程改为启发式，不再固定 4 步
        assert "分析方法" in prompt
        assert "按需执行" in prompt or "简单事实查询" in prompt

    def test_system_prompt_format_is_intent_aware(self) -> None:
        """system prompt 输出格式要求必须按查询性质分档，
        而非无差别套用"## 报告 + 摘要 + 建议"模板（见 docs/prompt_issue.md）。
        """
        from modules.p2p.prompts import build_system_prompt

        prompt = build_system_prompt()
        # 必须出现"按查询性质分档"或等价措辞
        assert "查询性质" in prompt or "查询类型" in prompt
        # 必须明确提到事实查询不要强加标题/建议段落
        assert "事实查询" in prompt or "状态确认" in prompt
        assert ("不要" in prompt and ("标题" in prompt or "建议" in prompt))
        # 历史 bug：旧 prompt "以 Markdown 格式组织报告，包含标题、摘要、详细发现和建议"
        # 一刀切要求所有回复带标题。修复后这条必须被分档化措辞替代。
        assert "以 Markdown 格式组织报告，包含标题、摘要、详细发现和建议" not in prompt

    def test_system_prompt_drops_tool_listing(self) -> None:
        """删除重复的工具编号清单（P2 - 3.1），避免与 LangChain 注入的 schema 漂移。"""
        from modules.p2p.prompts import build_system_prompt

        prompt = build_system_prompt()
        # 编号列表已删除（原来是 "1. **query_purchase_orders**" ... "8. **calculate_supplier_kpis**"）
        assert "1. **query_purchase_orders**" not in prompt
        assert "8. **calculate_supplier_kpis**" not in prompt
        # 但整体"工具使用"章节仍存在，给出高层引导
        assert "工具使用" in prompt

    def test_get_ontology_context_success(self) -> None:
        """本体上下文获取成功时应返回格式化文本。"""
        from modules.p2p.prompts import get_ontology_context

        context = get_ontology_context()
        # 应包含业务背景文本（成功或降级都可以）
        assert "P2P" in context or "采购" in context

    def test_get_ontology_context_failure(self) -> None:
        """本体加载失败时应返回默认文本。"""
        with patch("modules.p2p.prompts.OntologyLoader", side_effect=Exception("no owl")):
            from modules.p2p.prompts import get_ontology_context

            context = get_ontology_context()
        assert "三路匹配" in context

    def test_get_or_build_agent(self) -> None:
        """_get_or_build_agent 应构建并缓存 agent。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_langchain_agent = MagicMock()
        with (
            patch("modules.p2p.agent.build_chat_model"),
            patch("modules.p2p.agent.create_agent", return_value=mock_langchain_agent),
        ):
            result = agent._get_or_build_agent()
        assert result is mock_langchain_agent
        # 第二次调用应复用
        assert agent._get_or_build_agent() is mock_langchain_agent


class TestP2PAgentAnalyze:
    """Agent analyze 方法单元测试。"""

    async def test_analyze_with_json_content(self) -> None:
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
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [mock_message]})
        agent._agent = mock_agent

        result = await agent.analyze("分析三路匹配")
        assert result.status == AnalysisStatus.SUCCESS
        assert result.analysis_type == AnalysisType.THREE_WAY_MATCH

    async def test_analyze_with_empty_messages(self) -> None:
        """Agent 返回空消息列表时应正常处理。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
        agent._agent = mock_agent

        result = await agent.analyze("测试")
        assert result.status == AnalysisStatus.SUCCESS
        assert result.report_markdown == ""

    async def test_analyze_message_without_content_attr(self) -> None:
        """消息对象无 content 属性时应转为字符串。"""
        settings = Settings()
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_agent = MagicMock()
        # 使用字符串而非有 content 属性的对象
        mock_agent.ainvoke = AsyncMock(return_value={"messages": ["plain text"]})
        agent._agent = mock_agent

        result = await agent.analyze("测试")
        assert result.status == AnalysisStatus.SUCCESS

    async def test_analyze_long_term_memory_disabled(self) -> None:
        """long_term_enabled=False 时，不应触达 get_long_term_memory。"""
        settings = Settings()
        settings.memory.long_term_enabled = False
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_message = MagicMock()
        mock_message.content = "ok"
        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [mock_message]})
        agent._agent = mock_agent

        with patch("core.memory.get_long_term_memory") as mock_get_ltm:
            result = await agent.analyze("测试")

        assert result.status == AnalysisStatus.SUCCESS
        mock_get_ltm.assert_not_called()

    async def test_analyze_long_term_memory_enabled(self) -> None:
        """long_term_enabled=True 时，应调用 search_memories 与 save_memory。"""
        settings = Settings()
        settings.memory.long_term_enabled = True
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_message = MagicMock()
        mock_message.content = "ok"
        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [mock_message]})
        agent._agent = mock_agent

        mock_ltm = MagicMock()
        mock_ltm.search_memories.return_value = []
        with patch("core.memory.get_long_term_memory", return_value=mock_ltm):
            result = await agent.analyze("测试")

        assert result.status == AnalysisStatus.SUCCESS
        assert mock_ltm.search_memories.called
        assert mock_ltm.save_memory.called

    async def test_analyze_retry_then_success(self) -> None:
        """首次失败后重试成功。"""
        settings = Settings()
        settings.llm.max_retries = 2
        with patch("modules.p2p.agent.get_settings", return_value=settings):
            from modules.p2p.agent import P2PAgent
            agent = P2PAgent(settings=settings)

        mock_message = MagicMock()
        mock_message.content = "ok"
        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(
            side_effect=[
                RuntimeError("first fail"),
                {"messages": [mock_message]},
            ]
        )

        # 需要让 _get_or_build_agent 返回同一个 mock
        with (
            patch("modules.p2p.agent.build_chat_model"),
            patch("modules.p2p.agent.create_agent", return_value=mock_agent),
        ):
            result = await agent.analyze("测试")
        assert result.status == AnalysisStatus.SUCCESS


class TestBuildMemoryContent:
    """_build_memory_content 单元测试。"""

    def setup_method(self) -> None:
        from modules.p2p.agent import _build_memory_content
        self._fn = _build_memory_content

    def test_basic_no_summary(self) -> None:
        content = self._fn("查询三路匹配", "发现3个异常", {})
        assert content.startswith("Q: 查询三路匹配")
        assert "A: 发现3个异常" in content

    def test_summary_anomaly_count_included(self) -> None:
        content = self._fn("查询", "answer", {"anomaly_count": 5})
        assert "异常数量: 5" in content

    def test_summary_total_anomalies_fallback(self) -> None:
        content = self._fn("查询", "answer", {"total_anomalies": 3})
        assert "异常数量: 3" in content

    def test_summary_text_included(self) -> None:
        content = self._fn("查询", "answer", {"summary": "供应链正常"})
        assert "摘要: 供应链正常" in content

    def test_summary_description_fallback(self) -> None:
        content = self._fn("查询", "answer", {"description": "整体良好"})
        assert "摘要: 整体良好" in content

    def test_response_truncated(self) -> None:
        long_response = "x" * 2000
        content = self._fn("q", long_response, {})
        # 截断到 1500 字符
        assert content.count("x") == 1500

    def test_summary_text_truncated(self) -> None:
        long_summary = "X" * 600
        content = self._fn("q", "ans", {"summary": long_summary})
        # summary 字段截断到 300 字符（300 个 X）
        assert content.count("X") == 300

    def test_order_q_summary_a(self) -> None:
        content = self._fn("query", "answer", {"anomaly_count": 2, "summary": "摘要"})
        q_pos = content.index("Q:")
        sum_pos = content.index("摘要")
        a_pos = content.index("A:")
        assert q_pos < sum_pos < a_pos


class TestBuildMemoryMetadata:
    """_build_memory_metadata 单元测试。"""

    def setup_method(self) -> None:
        from modules.p2p.agent import _build_memory_metadata
        self._fn = _build_memory_metadata

    def _anomaly(self, supplier: str = "", po: str = "") -> dict:
        return {"documents": {"supplier_name": supplier, "po_number": po}}

    def test_basic_fields(self) -> None:
        meta = self._fn("q", "three_way_match", [], {}, 30)
        assert meta["query"] == "q"
        assert meta["analysis_type"] == "three_way_match"
        assert meta["anomaly_count"] == 0
        assert meta["time_range_days"] == 30
        assert meta["entities"]["suppliers"] == []
        assert meta["entities"]["po_numbers"] == []

    def test_anomaly_count_from_list(self) -> None:
        anomalies = [self._anomaly(), self._anomaly()]
        meta = self._fn("q", "t", anomalies, {}, 30)
        assert meta["anomaly_count"] == 2

    def test_entities_extracted(self) -> None:
        anomalies = [
            self._anomaly("Supplier A", "PO-001"),
            self._anomaly("Supplier B", "PO-002"),
            self._anomaly("Supplier A", "PO-001"),  # 重复
        ]
        meta = self._fn("q", "t", anomalies, {}, 30)
        assert meta["entities"]["suppliers"] == ["Supplier A", "Supplier B"]
        assert meta["entities"]["po_numbers"] == ["PO-001", "PO-002"]

    def test_entities_capped(self) -> None:
        from modules.p2p.agent import _MAX_ENTITIES_PER_TYPE
        anomalies = [
            self._anomaly(f"SUP-{i}", f"PO-{i}")
            for i in range(_MAX_ENTITIES_PER_TYPE + 5)
        ]
        meta = self._fn("q", "t", anomalies, {}, 30)
        assert len(meta["entities"]["suppliers"]) == _MAX_ENTITIES_PER_TYPE
        assert len(meta["entities"]["po_numbers"]) == _MAX_ENTITIES_PER_TYPE

    def test_summary_text_from_dict(self) -> None:
        meta = self._fn("q", "t", [], {"summary": "一切正常"}, 30)
        assert meta["summary"] == "一切正常"

    def test_summary_description_fallback(self) -> None:
        meta = self._fn("q", "t", [], {"description": "整体无异常"}, 30)
        assert meta["summary"] == "整体无异常"

    def test_summary_truncated(self) -> None:
        meta = self._fn("q", "t", [], {"summary": "x" * 600}, 30)
        assert len(meta["summary"]) == 500

    def test_empty_anomaly_supplier_skipped(self) -> None:
        anomalies = [self._anomaly("", "PO-001")]
        meta = self._fn("q", "t", anomalies, {}, 30)
        assert meta["entities"]["suppliers"] == []
        assert "PO-001" in meta["entities"]["po_numbers"]

    def test_none_anomalies(self) -> None:
        meta = self._fn("q", "t", None, {}, 30)
        assert meta["anomaly_count"] == 0
