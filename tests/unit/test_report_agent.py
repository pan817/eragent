"""ReportAgent 单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import Settings


@pytest.fixture()
def settings() -> Settings:
    return Settings()


class TestReportAgentInit:
    def test_lazy_llm_not_created_on_init(self, settings: Settings) -> None:
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        assert agent._llm is None

    def test_ensure_llm_caches(self, settings: Settings) -> None:
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        mock_llm = MagicMock()
        with patch("modules.p2p.model_factory.build_chat_model", return_value=mock_llm):
            result = agent._ensure_llm()
        assert result is mock_llm
        assert agent._ensure_llm() is mock_llm  # 第二次直接返回缓存

    def test_ensure_llm_passes_disable_thinking(self, settings: Settings) -> None:
        """_ensure_llm 应传 disable_thinking=True 给 build_chat_model。"""
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        with patch("modules.p2p.model_factory.build_chat_model") as mock_build:
            agent._ensure_llm()
            # 报告生成走小模型 llm_fast（未配置时在 from_yaml 里自动镜像 llm）
            mock_build.assert_called_once_with(settings.llm_fast, disable_thinking=True)


class TestReportAgentGenerate:
    @pytest.mark.asyncio
    async def test_generate_basic(self, settings: Settings) -> None:
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "# 分析报告\n\n报告内容"
        mock_response.usage_metadata = {"total_tokens": 100}
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        result = await agent.generate(
            scenario="三路匹配分析",
            outputs={"match_result": '{"anomalies": []}'},
        )
        assert "分析报告" in result
        mock_llm.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_no_long_term_in_prompt(self, settings: Settings) -> None:
        """报告生成 prompt 中不应包含历史记忆相关内容。"""
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "# 报告"
        mock_response.usage_metadata = None
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        result = await agent.generate(
            scenario="价格差异",
            outputs={"price_data": "{}"},
        )
        assert result == "# 报告"
        prompt = mock_llm.ainvoke.call_args[0][0]
        assert "历史记忆" not in prompt
        assert "趋势对比" not in prompt

    @pytest.mark.asyncio
    async def test_generate_with_output_mode(self, settings: Settings) -> None:
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "简报内容"
        mock_response.usage_metadata = None
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        await agent.generate(
            scenario="测试",
            outputs={"data": "{}"},
            output_mode_prompt="请简洁输出",
        )
        prompt = mock_llm.ainvoke.call_args[0][0]
        assert "请简洁输出" in prompt

    @pytest.mark.asyncio
    async def test_generate_skips_report_key(self, settings: Settings) -> None:
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "OK"
        mock_response.usage_metadata = None
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        await agent.generate(
            scenario="测试",
            outputs={"report": "should be skipped", "data": '{"a":1}'},
        )
        prompt = mock_llm.ainvoke.call_args[0][0]
        assert "should be skipped" not in prompt
        assert "data" in prompt

    @pytest.mark.asyncio
    async def test_generate_empty_outputs(self, settings: Settings) -> None:
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "无数据"
        mock_response.usage_metadata = None
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        await agent.generate(scenario="测试", outputs={})
        prompt = mock_llm.ainvoke.call_args[0][0]
        assert "无数据输出" in prompt

    @pytest.mark.asyncio
    async def test_generate_truncates_large_output(self, settings: Settings) -> None:
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "OK"
        mock_response.usage_metadata = None
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        big_value = "x" * 5000
        await agent.generate(scenario="测试", outputs={"big": big_value})
        prompt = mock_llm.ainvoke.call_args[0][0]
        # 应截断到 3000
        assert len(big_value) > 3000
        assert "x" * 3001 not in prompt

    @pytest.mark.asyncio
    async def test_generate_llm_failure_raises(self, settings: Settings) -> None:
        """非网络类异常直接抛 ReportGenerationError，不重试、不吞。"""
        from modules.p2p.report_agent import ReportAgent
        from modules.p2p.errors import ReportGenerationError

        agent = ReportAgent(settings=settings)
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        with pytest.raises(ReportGenerationError) as exc_info:
            await agent.generate(scenario="三路匹配", outputs={"data": "{}"})
        assert exc_info.value.code == "REPORT_GEN_FAILED"
        assert "LLM down" in exc_info.value.message
        # 非瞬时错误不重试，调用次数应为 1
        assert mock_llm.ainvoke.await_count == 1

    @pytest.mark.asyncio
    async def test_generate_response_without_content_attr(self, settings: Settings) -> None:
        """LLM 返回非标准对象（无 content 属性）时应 fallback 到 str()。"""
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value="plain string response")
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        result = await agent.generate(scenario="测试", outputs={"d": "{}"})
        assert "plain string response" in result

    @pytest.mark.asyncio
    async def test_transient_error_retries_then_succeeds(
        self, settings: Settings, monkeypatch
    ) -> None:
        """瞬时连接错误前两次失败、第三次成功 → 返回报告、总共调 3 次。"""
        import httpx

        from modules.p2p.report_agent import ReportAgent

        # 去掉指数退避等待，加快测试
        import modules.p2p.report_agent as ra_module
        from tenacity import wait_none

        monkeypatch.setattr(ra_module, "wait_exponential", lambda **_: wait_none())

        agent = ReportAgent(settings=settings)
        ok_response = MagicMock()
        ok_response.content = "# 成功报告"
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[
            httpx.ConnectError("conn reset"),
            httpx.ConnectError("conn reset"),
            ok_response,
        ])
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        result = await agent.generate(scenario="测试", outputs={"d": "{}"})
        assert "成功报告" in result
        assert mock_llm.ainvoke.await_count == 3

    @pytest.mark.asyncio
    async def test_transient_error_exhausts_raises_structured(
        self, settings: Settings, monkeypatch
    ) -> None:
        """3 次都失败 → 抛 ReportGenerationError(code=LLM_CONNECTION_ERROR)。"""
        import httpx

        import modules.p2p.report_agent as ra_module
        from modules.p2p.errors import ReportGenerationError
        from modules.p2p.report_agent import ReportAgent
        from tenacity import wait_none

        monkeypatch.setattr(ra_module, "wait_exponential", lambda **_: wait_none())

        agent = ReportAgent(settings=settings)
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(
            side_effect=httpx.ConnectError("conn reset")
        )
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        with pytest.raises(ReportGenerationError) as exc_info:
            await agent.generate(scenario="测试", outputs={"d": "{}"})
        assert exc_info.value.code == "LLM_CONNECTION_ERROR"
        assert mock_llm.ainvoke.await_count == 3
