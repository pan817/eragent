"""MemoryManager 单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
