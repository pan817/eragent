"""MemoryManager 单元测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import Settings
from core.memory.manager import MemoryManager


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def manager(settings: Settings) -> MemoryManager:
    with patch("core.memory.short_term.ShortTermMemory"):
        return MemoryManager(settings=settings)


class TestMemoryManagerInit:
    def test_creates_short_term(self, manager: MemoryManager) -> None:
        assert manager._short_term is not None

    def test_feedback_detector_lazy(self, manager: MemoryManager) -> None:
        assert manager._feedback_detector is None


class TestBuildContext:
    @pytest.mark.asyncio
    async def test_returns_empty_string_phase2(self, manager: MemoryManager) -> None:
        result = await manager.build_context("u1", "query", {})
        assert result == ""


class TestOnAnalysisComplete:
    def test_does_not_raise_when_disabled(self, manager: MemoryManager) -> None:
        manager._settings.memory.long_term_enabled = False
        manager._settings.memory.short_term_summary_enabled = False
        manager._settings.memory.consolidation.enabled = False
        manager.on_analysis_complete(MagicMock(trace_id="t1", user_id="u1", report_markdown=""), "s1", {})


class TestOnUserMessage:
    def test_skips_when_feedback_disabled(self, manager: MemoryManager) -> None:
        manager._settings.memory.feedback.enabled = False
        manager.on_user_message("test", "s1", {})

    def test_no_signal_no_task(self, manager: MemoryManager) -> None:
        manager.on_user_message("分析三路匹配", "s1", {})
        # 无反馈信号 → 不创建异步任务


class TestCheckpointerDelegation:
    def test_get_checkpointer(self, manager: MemoryManager) -> None:
        manager._short_term.get_checkpointer = MagicMock(return_value="cp")
        assert manager.get_checkpointer() == "cp"

    def test_ensure_checkpointer(self, manager: MemoryManager) -> None:
        manager._short_term.ensure_checkpointer = MagicMock(return_value=None)
        assert manager.ensure_checkpointer() is None

    def test_clear(self, manager: MemoryManager) -> None:
        manager._short_term.clear = MagicMock(return_value=1)
        assert manager.clear_short_term_memory("s1") == 1

    def test_close(self, manager: MemoryManager) -> None:
        manager._short_term.close = MagicMock()
        manager.close()
        manager._short_term.close.assert_called_once()


class TestSessionDelegation:
    def test_load_session(self, manager: MemoryManager) -> None:
        manager._short_term.load_session_context = MagicMock(
            return_value={"has_history": False}
        )
        ctx = manager.load_session("s1")
        assert ctx["has_history"] is False

    def test_save_entities(self, manager: MemoryManager) -> None:
        manager._short_term.save_entity_context = MagicMock()
        manager.save_session_entities("s1", {"po_number": "PO-001"})
        manager._short_term.save_entity_context.assert_called_once_with(
            "s1", {"po_number": "PO-001"}
        )


# ============================================================
# build_context — 覆盖 long_term_enabled=True 路径 (lines 49, 65-76)
# ============================================================


class TestBuildContextEnabled:
    """build_context 在 long_term_enabled=True 时的各种路径。"""

    @pytest.mark.asyncio
    async def test_returns_empty_when_disabled(self, manager: MemoryManager) -> None:
        """line 49: long_term_enabled=False 直接返回空字符串。"""
        manager._settings.memory.long_term_enabled = False
        result = await manager.build_context("u1", "query", {})
        assert result == ""

    @pytest.mark.asyncio
    async def test_success_returns_formatted(self, manager: MemoryManager) -> None:
        """正常路径：检索+格式化返回非空字符串。"""
        manager._settings.memory.long_term_enabled = True
        mock_span = {}

        with patch(
            "core.memory.manager.MemoryManager._search_and_format",
            new_callable=AsyncMock,
            return_value="<memory>context</memory>",
        ), patch(
            "core.observability.tracing.record_span",
        ) as mock_record_span:
            mock_record_span.return_value.__enter__ = MagicMock(return_value=mock_span)
            mock_record_span.return_value.__exit__ = MagicMock(return_value=False)

            result = await manager.build_context("u1", "query", {})
            assert result == "<memory>context</memory>"
            assert mock_span["status"] == "ok"
            assert mock_span["memory_context_chars"] == len("<memory>context</memory>")

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self, manager: MemoryManager) -> None:
        """lines 65-71: 超时时返回空字符串并记录 timeout 状态。"""
        manager._settings.memory.long_term_enabled = True
        manager._settings.memory.long_term_search_timeout_seconds = 0.001
        mock_span: dict = {}

        async def _slow_search(*args, **kwargs) -> str:
            await asyncio.sleep(10)
            return "never"

        with patch.object(
            manager, "_search_and_format", side_effect=_slow_search,
        ), patch(
            "core.observability.tracing.record_span",
        ) as mock_record_span:
            mock_record_span.return_value.__enter__ = MagicMock(return_value=mock_span)
            mock_record_span.return_value.__exit__ = MagicMock(return_value=False)

            result = await manager.build_context("u1", "query", {})
            assert result == ""
            assert mock_span["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self, manager: MemoryManager) -> None:
        """lines 72-76: 异常时返回空字符串并记录 error 状态。"""
        manager._settings.memory.long_term_enabled = True
        mock_span: dict = {}

        with patch.object(
            manager,
            "_search_and_format",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ), patch(
            "core.observability.tracing.record_span",
        ) as mock_record_span:
            mock_record_span.return_value.__enter__ = MagicMock(return_value=mock_span)
            mock_record_span.return_value.__exit__ = MagicMock(return_value=False)

            result = await manager.build_context("u1", "query", {})
            assert result == ""
            assert mock_span["status"] == "error"
            assert mock_span["error"] == "db down"


# ============================================================
# on_analysis_complete — 覆盖 summary 分支 (line 126)
# ============================================================


class TestOnAnalysisCompleteSummary:
    """on_analysis_complete 中 summary 触发分支。"""

    def test_creates_summary_task_when_report_exceeds_threshold(
        self, manager: MemoryManager,
    ) -> None:
        """line 126: report_markdown 超过阈值时创建摘要任务。"""
        manager._settings.memory.long_term_enabled = False
        manager._settings.memory.short_term_summary_enabled = True
        manager._settings.memory.short_term_summary_max_input_chars = 10
        manager._settings.memory.consolidation.enabled = False

        long_report = "x" * 100
        result = MagicMock(
            trace_id="t1", user_id="u1", report_markdown=long_report,
        )

        with patch("asyncio.create_task") as mock_create_task:
            manager.on_analysis_complete(result, "s1", {})
            # summary task should be created
            mock_create_task.assert_called_once()
            call_kwargs = mock_create_task.call_args
            assert "mem_summary_s1" in str(call_kwargs)


# ============================================================
# on_user_message — 覆盖有信号时创建任务 (line 155)
# ============================================================


class TestOnUserMessageWithSignal:
    """on_user_message 检测到反馈信号时创建异步任务。"""

    def test_creates_feedback_task_when_signal_detected(
        self, manager: MemoryManager,
    ) -> None:
        """line 155: 检测到信号后 create_task。"""
        manager._settings.memory.feedback.enabled = True

        mock_detector = MagicMock()
        mock_detector.detect_signal.return_value = "correction"
        manager._feedback_detector = mock_detector

        with patch("asyncio.create_task") as mock_create_task:
            manager.on_user_message("这个不对", "s1", {"user_id": "u1"})
            mock_create_task.assert_called_once()
            call_kwargs = mock_create_task.call_args
            assert "mem_feedback_s1" in str(call_kwargs)


# ============================================================
# _summarize_result (lines 221-231)
# ============================================================


class TestSummarizeResult:
    """_summarize_result 异步方法。"""

    @pytest.mark.asyncio
    async def test_summarize_success(self, manager: MemoryManager) -> None:
        """lines 221-229: 正常路径调用 summarize_and_replace。"""
        mock_llm = MagicMock()
        manager._short_term.summarize_and_replace = AsyncMock()

        with patch(
            "core.llm.model_factory.build_chat_model", return_value=mock_llm,
        ):
            await manager._summarize_result("long report...", "s1")
            manager._short_term.summarize_and_replace.assert_awaited_once_with(
                session_id="s1",
                ai_content="long report...",
                llm_fast=mock_llm,
            )

    @pytest.mark.asyncio
    async def test_summarize_exception_logged(self, manager: MemoryManager) -> None:
        """lines 230-231: 异常不传播，仅记录日志。"""
        with patch(
            "core.llm.model_factory.build_chat_model",
            side_effect=RuntimeError("llm fail"),
        ):
            # Should not raise
            await manager._summarize_result("report", "s1")


# ============================================================
# _extract_memories (lines 249-250)
# ============================================================


class TestExtractMemories:
    """_extract_memories 异步方法。"""

    @pytest.mark.asyncio
    async def test_extract_success(self, manager: MemoryManager) -> None:
        """正常路径：调用 extractor.extract。"""
        mock_extractor = MagicMock()
        mock_extractor.extract = AsyncMock(return_value=["outcome1"])

        result = MagicMock(user_id="u1", session_id="s1")

        with patch(
            "core.memory.extractor.MemoryExtractor", return_value=mock_extractor,
        ), patch("core.memory.long_term.get_memory_repository"):
            await manager._extract_memories(result)
            mock_extractor.extract.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_extract_exception_logged(self, manager: MemoryManager) -> None:
        """lines 249-250: 异常不传播。"""
        with patch(
            "core.memory.extractor.MemoryExtractor",
            side_effect=RuntimeError("extractor init fail"),
        ), patch("core.memory.long_term.get_memory_repository"):
            # Should not raise
            await manager._extract_memories(MagicMock(user_id="u1", session_id="s1"))


# ============================================================
# _extract_feedback (lines 260-314)
# ============================================================


class TestExtractFeedback:
    """_extract_feedback 异步方法。"""

    @pytest.mark.asyncio
    async def test_feedback_none_returns_early(self, manager: MemoryManager) -> None:
        """line 269-270: extract 返回 None 时提前返回。"""
        mock_detector = MagicMock()
        mock_detector.extract = AsyncMock(return_value=None)
        manager._feedback_detector = mock_detector

        with patch("core.llm.model_factory.build_chat_model"), \
             patch("core.memory.long_term.get_memory_repository"):
            await manager._extract_feedback("query", "s1", {"user_id": "u1"}, "correction")

    @pytest.mark.asyncio
    async def test_feedback_low_confidence_returns_early(
        self, manager: MemoryManager,
    ) -> None:
        """lines 272-274: 置信度低于阈值时提前返回。"""
        manager._settings.memory.feedback.min_confidence = 0.6

        mock_detector = MagicMock()
        mock_detector.extract = AsyncMock(return_value={
            "confidence": 0.3,
            "type": "correction",
            "content": "test",
        })
        manager._feedback_detector = mock_detector

        mock_repo = MagicMock()

        with patch("core.llm.model_factory.build_chat_model"), \
             patch("core.memory.long_term.get_memory_repository", return_value=mock_repo):
            await manager._extract_feedback("query", "s1", {"user_id": "u1"}, "correction")
            mock_repo.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_feedback_unknown_type_returns_early(
        self, manager: MemoryManager,
    ) -> None:
        """lines 282-284: 未知反馈类型时提前返回。"""
        manager._settings.memory.feedback.min_confidence = 0.5

        mock_detector = MagicMock()
        mock_detector.extract = AsyncMock(return_value={
            "confidence": 0.9,
            "type": "unknown_type",
            "content": "test",
        })
        manager._feedback_detector = mock_detector

        mock_repo = MagicMock()

        with patch("core.llm.model_factory.build_chat_model"), \
             patch("core.memory.long_term.get_memory_repository", return_value=mock_repo):
            await manager._extract_feedback("query", "s1", {"user_id": "u1"}, "correction")
            mock_repo.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_feedback_correction_saved(self, manager: MemoryManager) -> None:
        """lines 286-312: 完整保存路径 — correction 类型。"""
        from core.memory.types import MemoryType

        manager._settings.memory.feedback.min_confidence = 0.5

        mock_detector = MagicMock()
        mock_detector.extract = AsyncMock(return_value={
            "confidence": 0.8,
            "type": "correction",
            "content": "PO-001 should be matched",
            "related_entities": {"vendor_id": "V100", "po_number": "PO-001"},
        })
        manager._feedback_detector = mock_detector

        mock_repo = MagicMock()
        mock_repo.compute_expires_at.return_value = None

        with patch("core.llm.model_factory.build_chat_model"), \
             patch("core.memory.long_term.get_memory_repository", return_value=mock_repo):
            await manager._extract_feedback(
                "这个不对", "s1", {"user_id": "u1"}, "correction",
            )
            mock_repo.save.assert_called_once()
            call_kwargs = mock_repo.save.call_args[1]
            assert call_kwargs["user_id"] == "u1"
            assert call_kwargs["session_id"] == "s1"
            assert call_kwargs["memory_type"] == MemoryType.CORRECTION
            assert call_kwargs["content"] == "PO-001 should be matched"
            assert call_kwargs["entity_id"] == "V100"  # vendor_id takes priority

    @pytest.mark.asyncio
    async def test_feedback_user_preference_saved(self, manager: MemoryManager) -> None:
        """lines 277-281: user_preference 类型映射正确。"""
        from core.memory.types import MemoryType

        manager._settings.memory.feedback.min_confidence = 0.5

        mock_detector = MagicMock()
        mock_detector.extract = AsyncMock(return_value={
            "confidence": 0.9,
            "type": "user_preference",
            "content": "always use table format",
            "related_entities": {},
        })
        manager._feedback_detector = mock_detector

        mock_repo = MagicMock()
        mock_repo.compute_expires_at.return_value = None

        with patch("core.llm.model_factory.build_chat_model"), \
             patch("core.memory.long_term.get_memory_repository", return_value=mock_repo):
            await manager._extract_feedback(
                "以后用表格", "s1", {"user_id": "u1"}, "preference",
            )
            call_kwargs = mock_repo.save.call_args[1]
            assert call_kwargs["memory_type"] == MemoryType.USER_PREFERENCE
            assert call_kwargs["entity_id"] is None  # no entities

    @pytest.mark.asyncio
    async def test_feedback_domain_fact_with_po_entity(
        self, manager: MemoryManager,
    ) -> None:
        """entity_id 回退到 po_number（vendor_id 为空时）。"""
        from core.memory.types import MemoryType

        manager._settings.memory.feedback.min_confidence = 0.5

        mock_detector = MagicMock()
        mock_detector.extract = AsyncMock(return_value={
            "confidence": 0.9,
            "type": "domain_fact",
            "content": "tolerance is 5%",
            "related_entities": {"po_number": "PO-999"},
        })
        manager._feedback_detector = mock_detector

        mock_repo = MagicMock()
        mock_repo.compute_expires_at.return_value = None

        with patch("core.llm.model_factory.build_chat_model"), \
             patch("core.memory.long_term.get_memory_repository", return_value=mock_repo):
            await manager._extract_feedback(
                "容差是5%", "s1", {"user_id": "u1"}, "fact",
            )
            call_kwargs = mock_repo.save.call_args[1]
            assert call_kwargs["memory_type"] == MemoryType.DOMAIN_FACT
            assert call_kwargs["entity_id"] == "PO-999"

    @pytest.mark.asyncio
    async def test_feedback_exception_logged(self, manager: MemoryManager) -> None:
        """lines 313-314: 异常不传播。"""
        manager._feedback_detector = MagicMock()
        manager._feedback_detector.extract = AsyncMock(
            side_effect=RuntimeError("boom"),
        )

        with patch("core.llm.model_factory.build_chat_model"):
            # Should not raise
            await manager._extract_feedback(
                "query", "s1", {"user_id": "u1"}, "correction",
            )
