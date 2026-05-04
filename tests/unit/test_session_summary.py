"""SessionSummaryExtractor + MemoryManager.on_session_idle 单元测试。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import Settings
from core.memory.session_summary import SessionSummaryExtractor


@pytest.fixture
def settings() -> Settings:
    s = Settings()
    s.memory.session_recap.enabled = True
    s.memory.session_recap.min_messages = 4
    s.memory.session_recap.min_confidence = 0.6
    s.memory.session_recap.summary_max_chars = 500
    s.memory.session_recap.summary_timeout_seconds = 30
    return s


@pytest.fixture
def extractor(settings: Settings) -> SessionSummaryExtractor:
    return SessionSummaryExtractor(settings)


# ── extract 基本流程 ───────────────────────────────────────────


class TestExtractBasic:
    @pytest.mark.asyncio
    async def test_skips_when_too_few_messages(self, extractor: SessionSummaryExtractor) -> None:
        few_messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        with patch.object(
            extractor, "_load_session_data", return_value=(few_messages, {}),
        ):
            result = await extractor.extract("s1", "u1")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_extraction(self, extractor: SessionSummaryExtractor) -> None:
        messages = [
            {"role": "user", "content": f"msg{i}"} for i in range(6)
        ]
        llm_response = MagicMock()
        llm_response.content = json.dumps({
            "summary_text": "SUP-001 的三路匹配分析结果正常",
            "key_entities": {"vendor_id": "SUP-001"},
            "tags": ["三路匹配", "SUP-001"],
            "conclusion": "无重大异常",
            "confidence": 0.85,
        })

        with (
            patch.object(extractor, "_load_session_data", return_value=(messages, {})),
            patch("core.llm.model_factory.build_chat_model", return_value=MagicMock(invoke=lambda p: llm_response)),
            patch.object(extractor, "_write_to_stores", return_value="mem-001"),
        ):
            result = await extractor.extract("s1", "u1")

        assert result is not None
        assert result["session_id"] == "s1"
        assert result["memory_id"] == "mem-001"
        assert result["confidence"] == 0.85
        assert "SUP-001" in result["summary_text"]

    @pytest.mark.asyncio
    async def test_returns_none_on_parse_error(self, extractor: SessionSummaryExtractor) -> None:
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(6)]
        llm_response = MagicMock()
        llm_response.content = "this is not json"

        with (
            patch.object(extractor, "_load_session_data", return_value=(messages, {})),
            patch("core.llm.model_factory.build_chat_model", return_value=MagicMock(invoke=lambda p: llm_response)),
        ):
            result = await extractor.extract("s1", "u1")

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, settings: Settings) -> None:
        settings.memory.session_recap.summary_timeout_seconds = 0.001
        ext = SessionSummaryExtractor(settings)

        async def slow_impl(*a, **kw):
            await asyncio.sleep(5)
            return None

        with patch.object(ext, "_extract_impl", slow_impl):
            result = await ext.extract("s1", "u1")
        assert result is None


# ── confidence 门槛 ────────────────────────────────────────────


class TestConfidenceThreshold:
    @pytest.mark.asyncio
    async def test_low_confidence_skips_chroma(self, extractor: SessionSummaryExtractor) -> None:
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(6)]
        llm_response = MagicMock()
        llm_response.content = json.dumps({
            "summary_text": "零散对话",
            "key_entities": {},
            "tags": [],
            "conclusion": "",
            "confidence": 0.4,
        })

        write_calls: list[dict] = []

        def mock_write(**kwargs):
            write_calls.append(kwargs)
            return "mem-002"

        with (
            patch.object(extractor, "_load_session_data", return_value=(messages, {})),
            patch("core.llm.model_factory.build_chat_model", return_value=MagicMock(invoke=lambda p: llm_response)),
            patch.object(extractor, "_write_to_stores", side_effect=mock_write),
        ):
            result = await extractor.extract("s1", "u1")

        assert result is not None
        assert write_calls[0]["index_to_chroma"] is False

    @pytest.mark.asyncio
    async def test_high_confidence_indexes_chroma(self, extractor: SessionSummaryExtractor) -> None:
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(6)]
        llm_response = MagicMock()
        llm_response.content = json.dumps({
            "summary_text": "高质量摘要",
            "key_entities": {"vendor_id": "SUP-002"},
            "tags": ["供应商绩效"],
            "conclusion": "绩效达标",
            "confidence": 0.9,
        })

        write_calls: list[dict] = []

        def mock_write(**kwargs):
            write_calls.append(kwargs)
            return "mem-003"

        with (
            patch.object(extractor, "_load_session_data", return_value=(messages, {})),
            patch("core.llm.model_factory.build_chat_model", return_value=MagicMock(invoke=lambda p: llm_response)),
            patch.object(extractor, "_write_to_stores", side_effect=mock_write),
        ):
            result = await extractor.extract("s1", "u1")

        assert result is not None
        assert write_calls[0]["index_to_chroma"] is True


# ── _parse_response ────────────────────────────────────────────


class TestParseResponse:
    def test_valid_json(self) -> None:
        content = json.dumps({
            "summary_text": "ok",
            "key_entities": {},
            "tags": [],
            "conclusion": "",
            "confidence": 0.8,
        })
        result = SessionSummaryExtractor._parse_response(content)
        assert result is not None
        assert result["summary_text"] == "ok"

    def test_markdown_wrapped_json(self) -> None:
        content = '```json\n{"summary_text": "ok", "confidence": 0.8}\n```'
        result = SessionSummaryExtractor._parse_response(content)
        assert result is not None

    def test_invalid_json_returns_none(self) -> None:
        assert SessionSummaryExtractor._parse_response("not json") is None

    def test_missing_summary_text_returns_none(self) -> None:
        content = json.dumps({"confidence": 0.8})
        assert SessionSummaryExtractor._parse_response(content) is None


# ── _format_messages ───────────────────────────────────────────


class TestFormatMessages:
    def test_basic_format(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = SessionSummaryExtractor._format_messages(messages)
        assert "[user] hello" in result
        assert "[assistant] hi" in result

    def test_truncates_long_content(self) -> None:
        messages = [{"role": "user", "content": "x" * 1000}]
        result = SessionSummaryExtractor._format_messages(messages)
        assert len(result.split("] ")[1]) <= 500

    def test_limits_to_50_messages(self) -> None:
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(100)]
        result = SessionSummaryExtractor._format_messages(messages)
        lines = result.strip().split("\n")
        assert len(lines) == 50


# ── MemoryManager.on_session_idle ──────────────────────────────


class TestManagerOnSessionIdle:
    @pytest.mark.asyncio
    async def test_disabled_skips(self) -> None:
        settings = Settings()
        settings.memory.session_recap.enabled = False

        with patch("core.memory.short_term.ShortTermMemory"):
            from core.memory.manager import MemoryManager

            mgr = MemoryManager(settings=settings)

        mock_summarizer = MagicMock()
        mgr._session_summarizer = mock_summarizer
        await mgr.on_session_idle("s1", "u1")
        mock_summarizer.extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_delegates_to_summarizer(self) -> None:
        settings = Settings()
        settings.memory.session_recap.enabled = True

        with patch("core.memory.short_term.ShortTermMemory"):
            from core.memory.manager import MemoryManager

            mgr = MemoryManager(settings=settings)

        mock_summarizer = AsyncMock()
        mock_summarizer.extract = AsyncMock(return_value={"memory_id": "m1"})
        mgr._session_summarizer = mock_summarizer
        await mgr.on_session_idle("s1", "u1")
        mock_summarizer.extract.assert_called_once_with(
            session_id="s1", user_id="u1",
        )

    @pytest.mark.asyncio
    async def test_exception_swallowed(self) -> None:
        settings = Settings()
        settings.memory.session_recap.enabled = True

        with patch("core.memory.short_term.ShortTermMemory"):
            from core.memory.manager import MemoryManager

            mgr = MemoryManager(settings=settings)

        mock_summarizer = AsyncMock()
        mock_summarizer.extract = AsyncMock(side_effect=RuntimeError("db down"))
        mgr._session_summarizer = mock_summarizer
        await mgr.on_session_idle("s1", "u1")


# ── IdleSessionWatcher ─────────────────────────────────────────


class TestIdleSessionWatcher:
    def test_find_idle_sessions_returns_empty_on_error(self) -> None:
        from core.memory.idle_watcher import IdleSessionWatcher

        settings = Settings()
        watcher = IdleSessionWatcher(settings)
        with patch("core.database.engine.get_engine", side_effect=RuntimeError("no db")):
            result = watcher._find_idle_sessions()
        assert result == []


# ── injection.py SESSION_RECAP 区块 ────────────────────────────


class TestInjectionSessionRecap:
    def test_session_recap_section_included(self) -> None:
        from core.memory.injection import format_memory_injection

        memories = {
            "session_recap": [
                {"content": "上次讨论了SUP-001的三路匹配，结论是匹配率85%"},
            ],
        }
        result = format_memory_injection(memories)
        assert "[相关历史会话回顾]" in result
        assert "SUP-001" in result

    def test_empty_session_recap_not_shown(self) -> None:
        from core.memory.injection import format_memory_injection

        memories = {"session_recap": []}
        result = format_memory_injection(memories)
        assert "[相关历史会话回顾]" not in result

    def test_session_recap_ordered_before_user_preference(self) -> None:
        from core.memory.injection import format_memory_injection

        memories = {
            "session_recap": [{"content": "recap content"}],
            "user_preference": [{"content": "pref content"}],
        }
        result = format_memory_injection(memories)
        recap_pos = result.index("[相关历史会话回顾]")
        pref_pos = result.index("[用户偏好]")
        assert recap_pos < pref_pos
