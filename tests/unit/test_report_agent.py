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
        with patch("core.llm.model_factory.build_chat_model", return_value=mock_llm):
            result = agent._ensure_llm()
        assert result is mock_llm
        assert agent._ensure_llm() is mock_llm  # 第二次直接返回缓存

    def test_ensure_llm_passes_disable_thinking(self, settings: Settings) -> None:
        """_ensure_llm 应传 disable_thinking=True 给 build_chat_model。"""
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        with patch("core.llm.model_factory.build_chat_model") as mock_build:
            agent._ensure_llm()
            # 报告生成走小模型 llm_fast（未配置时在 from_yaml 里自动镜像 llm）；
            # max_tokens_override 取自 settings.report.max_output_tokens，
            # 0 时传 None，沿用 llm_fast.max_tokens。
            expected_override = settings.report.max_output_tokens or None
            mock_build.assert_called_once_with(
                settings.llm_fast,
                disable_thinking=True,
                max_tokens_override=expected_override,
            )


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

        limit = settings.report.outputs_per_key_max_chars
        big_value = "x" * (limit + 2000)
        await agent.generate(scenario="测试", outputs={"big": big_value})
        prompt = mock_llm.ainvoke.call_args[0][0]
        assert len(big_value) > limit
        # 超过上限的尾部字符不应出现在 prompt 中
        assert "x" * (limit + 1) not in prompt
        # 截断不向 prompt 注入"已截断"标记（只记录到 span attr）
        assert "已截断" not in prompt

    @pytest.mark.asyncio
    async def test_prompt_contains_severity_thresholds(self, settings: Settings) -> None:
        """prompt 中应注入 AnomalySeverity 配置的具体阈值，不是写死在模板里。"""
        from modules.p2p.report_agent import ReportAgent

        settings.p2p.anomaly_severity.high_amount_threshold = 800000.0
        settings.p2p.anomaly_severity.variance_high_multiplier = 3.0

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "# 报告"
        mock_response.usage_metadata = None
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        await agent.generate(scenario="测试", outputs={"d": "{}"})
        prompt = mock_llm.ainvoke.call_args[0][0]

        assert "800,000" in prompt
        assert "容差的 3 倍以上" in prompt

    @pytest.mark.asyncio
    async def test_prompt_contains_p0_guardrails(self, settings: Settings) -> None:
        """prompt 应包含数据诚信、只读边界、严重等级规则等 P0 约束。"""
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "# 报告"
        mock_response.usage_metadata = None
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        await agent.generate(scenario="测试", outputs={"d": "{}"})
        prompt = mock_llm.ainvoke.call_args[0][0]

        assert "工具输出数据" in prompt
        assert ("不得编造" in prompt) or ("不得虚构" in prompt) or ("禁止推断" in prompt)
        assert "只读" in prompt
        assert "建议人工处理" in prompt
        assert "HIGH" in prompt and "MEDIUM" in prompt and "LOW" in prompt

    @pytest.mark.asyncio
    async def test_prompt_contains_current_date(self, settings: Settings) -> None:
        """prompt 应注入当前日期与时区（P1：相对时间基准）。"""
        import re
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "# 报告"
        mock_response.usage_metadata = None
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        await agent.generate(scenario="测试", outputs={"d": "{}"})
        prompt = mock_llm.ainvoke.call_args[0][0]

        # YYYY-MM-DD 日期
        assert re.search(r"\d{4}-\d{2}-\d{2}", prompt)
        assert "Asia/Shanghai" in prompt
        assert "时间上下文" in prompt

    @pytest.mark.asyncio
    async def test_output_mode_overrides_core_requirements(
        self, settings: Settings
    ) -> None:
        """output_mode 段应明确标注优先级高于上文报告要求（P1：brief/core 冲突）。"""
        from modules.p2p.report_agent import ReportAgent

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "简报"
        mock_response.usage_metadata = None
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        await agent.generate(
            scenario="测试",
            outputs={"d": "{}"},
            output_mode_prompt="请以简报形式输出",
        )
        prompt = mock_llm.ainvoke.call_args[0][0]

        # 优先级声明
        assert "优先级高于" in prompt
        assert "以本节为准" in prompt

    @pytest.mark.asyncio
    async def test_span_records_prompt_hash(
        self, settings: Settings, monkeypatch
    ) -> None:
        """report.prep / report span 应记录 prompt_hash（P2 - 5.2 可观测性）。"""
        import hashlib
        from modules.p2p.report_agent import ReportAgent

        captured_attrs: list[dict] = []

        class _SpanCtx:
            def __init__(self, span_name: str, op: str) -> None:
                self.attrs: dict = {}
                captured_attrs.append(self.attrs)
            def __enter__(self) -> dict:
                return self.attrs
            def __exit__(self, *_: object) -> None:
                pass

        import core.observability.middleware as obs_mod
        monkeypatch.setattr(obs_mod, "record_span", _SpanCtx)

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "# 报告"
        mock_response.usage_metadata = None
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        await agent.generate(scenario="测试", outputs={"d": "{}"})

        # prep span 与 outer report span 均应带 prompt_hash
        hashes = [a.get("prompt_hash") for a in captured_attrs if "prompt_hash" in a]
        assert len(hashes) >= 2
        # hash 为 12 位 hex
        for h in hashes:
            assert isinstance(h, str) and len(h) == 12
            int(h, 16)  # 合法 hex
        # 两处 hash 应一致（同一 prompt）
        assert hashes[0] == hashes[-1]

    @pytest.mark.asyncio
    async def test_truncation_recorded_in_span_not_prompt(
        self, settings: Settings, monkeypatch
    ) -> None:
        """超限输出应静默截断并在 span attr 中计数，不向 prompt 注入"已截断"标记。"""
        from modules.p2p.report_agent import ReportAgent

        captured_attrs: list[dict] = []

        class _SpanCtx:
            def __init__(self, span_name: str, op: str) -> None:
                self.attrs: dict = {}
                captured_attrs.append(self.attrs)
            def __enter__(self) -> dict:
                return self.attrs
            def __exit__(self, *_: object) -> None:
                pass

        import core.observability.middleware as obs_mod
        monkeypatch.setattr(obs_mod, "record_span", _SpanCtx)

        agent = ReportAgent(settings=settings)
        mock_response = MagicMock()
        mock_response.content = "OK"
        mock_response.usage_metadata = None
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.model_name = "test-model"
        agent._llm = mock_llm

        limit = settings.report.outputs_per_key_max_chars
        big_value = "x" * (limit + 2000)
        await agent.generate(scenario="测试", outputs={"big": big_value})
        prompt = mock_llm.ainvoke.call_args[0][0]

        assert "已截断" not in prompt
        truncated_counts = [a.get("truncated_outputs") for a in captured_attrs
                            if "truncated_outputs" in a]
        assert truncated_counts and max(truncated_counts) == 1

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
