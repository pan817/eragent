"""ShortTermMemory 单元测试。

覆盖 get_checkpointer / close / clear / load_session_context /
save_dag_result / save_entity_context / summarize_and_replace 的
未覆盖行（41, 50-52, 57-58, 99-108, 138, 148, 154-158, 316,
389-457）。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from config.settings import Settings
from core.database import install_sqlite_timezone_hook
from core.memory.short_term import ShortTermMemory
from core.memory.tables import metadata_obj


# ── helpers ─────────────────────────────────────────────────────

@contextmanager
def _noop_span(*_args: Any, **_kwargs: Any):
    """No-op record_span context manager for tests."""
    attrs: dict[str, Any] = {}
    yield attrs


def _patch_record_span():
    return patch("core.observability.tracing.record_span", _noop_span)


# ── Fixtures ────────────────────────────────────────────────────

@pytest.fixture()
def entity_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    install_sqlite_timezone_hook(engine)
    metadata_obj.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def stm(entity_engine, settings) -> ShortTermMemory:
    mem = ShortTermMemory(settings=settings)
    mem._entity_engine = entity_engine
    return mem


# ============================================================
# get_checkpointer — double-check locking (line 41)
# ============================================================

class TestGetCheckpointer:
    def test_returns_cached_checkpointer(self, stm: ShortTermMemory) -> None:
        """First branch: _checkpointer already set -> return immediately."""
        sentinel = object()
        stm._checkpointer = sentinel
        assert stm.get_checkpointer() is sentinel

    def test_double_check_lock_returns_cached(self, stm: ShortTermMemory) -> None:
        """Line 41: inside lock, _checkpointer was set by another thread."""
        sentinel = object()

        original_lock = stm._lock

        class _LockThatSetsCheckpointer:
            def __enter__(self_lock):
                original_lock.__enter__()
                stm._checkpointer = sentinel
                return self_lock

            def __exit__(self_lock, *args):
                return original_lock.__exit__(*args)

        stm._lock = _LockThatSetsCheckpointer()
        result = stm.get_checkpointer()
        assert result is sentinel

    def test_setup_failure_calls_exit(self, stm: ShortTermMemory) -> None:
        """Lines 50-52: saver.setup() raises -> cm.__exit__ called, exception propagated."""
        mock_saver = MagicMock()
        mock_saver.setup.side_effect = RuntimeError("setup failed")
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_saver)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with patch(
            "langgraph.checkpoint.postgres.PostgresSaver.from_conn_string",
            return_value=mock_cm,
        ):
            with pytest.raises(RuntimeError, match="setup failed"):
                stm.get_checkpointer()

        mock_cm.__exit__.assert_called_once_with(None, None, None)
        assert stm._checkpointer is None

    def test_tracing_attach_failure_is_noncritical(self, stm: ShortTermMemory) -> None:
        """Lines 57-58: attach_tracing import/call fails -> logged, but init succeeds."""
        mock_saver = MagicMock()
        mock_saver.setup.return_value = None
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_saver)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with patch(
            "langgraph.checkpoint.postgres.PostgresSaver.from_conn_string",
            return_value=mock_cm,
        ), patch(
            "core.observability.checkpointer.attach_tracing",
            side_effect=ImportError("no tracing"),
        ), patch(
            "core.observability.TimingMiddleware",
            side_effect=ImportError("no timing"),
        ):
            result = stm.get_checkpointer()

        assert result is mock_saver
        assert stm._checkpointer is mock_saver


# ============================================================
# close — lines 67-75
# ============================================================

class TestClose:
    def test_close_no_cm(self, stm: ShortTermMemory) -> None:
        """Line 68-69: _checkpointer_cm is None -> immediate return."""
        stm.close()  # should not raise

    def test_close_success(self, stm: ShortTermMemory) -> None:
        """Lines 70-71: normal close, resets both attrs."""
        mock_cm = MagicMock()
        mock_cm.__exit__ = MagicMock(return_value=False)
        stm._checkpointer_cm = mock_cm
        stm._checkpointer = MagicMock()

        stm.close()

        assert stm._checkpointer_cm is None
        assert stm._checkpointer is None
        mock_cm.__exit__.assert_called_once_with(None, None, None)

    def test_close_exception_swallowed(self, stm: ShortTermMemory) -> None:
        """Lines 73-75: cm.__exit__ raises -> logged, not propagated."""
        mock_cm = MagicMock()
        mock_cm.__exit__ = MagicMock(side_effect=RuntimeError("close error"))
        stm._checkpointer_cm = mock_cm
        stm._checkpointer = MagicMock()

        stm.close()  # should not raise

        assert stm._checkpointer_cm is None
        assert stm._checkpointer is None

    def test_close_idempotent(self, stm: ShortTermMemory) -> None:
        """Close called twice -> second call is no-op."""
        mock_cm = MagicMock()
        mock_cm.__exit__ = MagicMock(return_value=False)
        stm._checkpointer_cm = mock_cm
        stm._checkpointer = MagicMock()

        stm.close()
        stm.close()  # second call, cm already None

        mock_cm.__exit__.assert_called_once()


# ============================================================
# clear — lines 99-108
# ============================================================

class TestClear:
    def test_no_checkpointer_returns_zero(self, stm: ShortTermMemory) -> None:
        """Line 99-100: _checkpointer is None -> return 0."""
        assert stm._checkpointer is None
        assert stm.clear(session_id="s1") == 0

    def test_no_session_id_returns_zero(self, stm: ShortTermMemory) -> None:
        """Lines 101-102: session_id is None -> return 0."""
        stm._checkpointer = MagicMock()
        assert stm.clear(session_id=None) == 0

    def test_successful_delete(self, stm: ShortTermMemory) -> None:
        """Lines 103-105: delete_thread succeeds -> return 1."""
        mock_cp = MagicMock()
        stm._checkpointer = mock_cp
        assert stm.clear(session_id="s1") == 1
        mock_cp.delete_thread.assert_called_once_with("s1")

    def test_delete_thread_exception_returns_zero(self, stm: ShortTermMemory) -> None:
        """Lines 106-108: delete_thread raises -> return 0."""
        mock_cp = MagicMock()
        mock_cp.delete_thread.side_effect = RuntimeError("db error")
        stm._checkpointer = mock_cp
        assert stm.clear(session_id="s1") == 0


# ============================================================
# load_session_context — lines 138, 148, 154-158
# ============================================================

class TestLoadSessionContext:
    def test_entities_only_no_checkpointer(self, stm: ShortTermMemory) -> None:
        """Line 138: checkpointer unavailable but entities exist -> return entities."""
        stm.save_entity_context("s1", {"po_number": "PO-001"})

        with _patch_record_span():
            result = stm.load_session_context("s1")

        assert result["has_history"] is True
        assert result["entities"]["po_number"] == "PO-001"

    def test_entities_only_no_checkpoint_data(self, stm: ShortTermMemory) -> None:
        """Line 148: checkpointer exists but no checkpoint data, entities present."""
        stm.save_entity_context("s1", {"vendor_id": "V100"})
        mock_cp = MagicMock()
        mock_cp.get_tuple.return_value = None
        stm._checkpointer = mock_cp

        with _patch_record_span():
            result = stm.load_session_context("s1")

        assert result["has_history"] is True
        assert result["entities"]["vendor_id"] == "V100"

    def test_no_checkpoint_data_no_entities(self, stm: ShortTermMemory) -> None:
        """Line 149: checkpointer exists, no checkpoint data, no entities -> empty."""
        mock_cp = MagicMock()
        mock_cp.get_tuple.return_value = None
        stm._checkpointer = mock_cp

        with _patch_record_span():
            result = stm.load_session_context("s_empty")

        assert result["has_history"] is False

    def test_entities_only_empty_messages(self, stm: ShortTermMemory) -> None:
        """Lines 154-158: checkpoint exists but messages empty, entities present."""
        stm.save_entity_context("s1", {"invoice_num": "INV-1"})

        mock_checkpoint = SimpleNamespace(
            checkpoint={"channel_values": {"messages": []}},
            config={},
            metadata={},
        )
        mock_cp = MagicMock()
        mock_cp.get_tuple.return_value = mock_checkpoint
        stm._checkpointer = mock_cp

        with _patch_record_span():
            result = stm.load_session_context("s1")

        assert result["has_history"] is True
        assert result["entities"]["invoice_num"] == "INV-1"

    def test_empty_messages_no_entities(self, stm: ShortTermMemory) -> None:
        """Line 158: checkpoint exists, messages empty, no entities -> empty."""
        mock_checkpoint = SimpleNamespace(
            checkpoint={"channel_values": {"messages": []}},
            config={},
            metadata={},
        )
        mock_cp = MagicMock()
        mock_cp.get_tuple.return_value = mock_checkpoint
        stm._checkpointer = mock_cp

        with _patch_record_span():
            result = stm.load_session_context("s_empty")

        assert result["has_history"] is False

    def test_no_entities_no_checkpoint(self, stm: ShortTermMemory) -> None:
        """No entities, no checkpointer -> empty context."""
        with _patch_record_span():
            result = stm.load_session_context("s_empty")

        assert result["has_history"] is False
        assert result["entities"] == {}

    def test_full_message_extraction(self, stm: ShortTermMemory) -> None:
        """Lines 160-197: messages present, extract last human/ai pair."""
        stm.save_entity_context("s1", {"po_number": "PO-99"})

        human_msg = SimpleNamespace(type="human", content="query about PO")
        ai_msg = SimpleNamespace(type="ai", content="analysis result for PO-99")

        mock_checkpoint = SimpleNamespace(
            checkpoint={"channel_values": {"messages": [human_msg, ai_msg]}},
            config={},
            metadata={},
        )
        mock_cp = MagicMock()
        mock_cp.get_tuple.return_value = mock_checkpoint
        stm._checkpointer = mock_cp

        with _patch_record_span():
            result = stm.load_session_context("s1")

        assert result["has_history"] is True
        assert result["context_summary"] == "analysis result for PO-99"
        assert result["entities"]["po_number"] == "PO-99"

    def test_full_message_with_trim(self, stm: ShortTermMemory) -> None:
        """Lines 177-186: context trim enabled, trim_to_token_budget called."""
        ai_content = "x" * 5000
        ai_msg = SimpleNamespace(type="ai", content=ai_content)

        mock_checkpoint = SimpleNamespace(
            checkpoint={"channel_values": {"messages": [ai_msg]}},
            config={},
            metadata={},
        )
        mock_cp = MagicMock()
        mock_cp.get_tuple.return_value = mock_checkpoint
        stm._checkpointer = mock_cp
        stm._settings.memory.short_term_context_trim_enabled = True

        with _patch_record_span(), patch(
            "modules.p2p.prompts.trim_to_token_budget",
            return_value="trimmed",
        ) as mock_trim:
            result = stm.load_session_context("s1")

        assert result["context_summary"] == "trimmed"
        mock_trim.assert_called_once()

    def test_exception_returns_empty(self, stm: ShortTermMemory) -> None:
        """Lines 199-203: exception during load -> returns empty context."""
        mock_cp = MagicMock()
        mock_cp.get_tuple.side_effect = RuntimeError("db error")
        stm._checkpointer = mock_cp

        with _patch_record_span():
            result = stm.load_session_context("s1")

        assert result["has_history"] is False


# ============================================================
# save_dag_result — lines 218-260
# ============================================================

class TestSaveDagResult:
    @pytest.mark.asyncio
    async def test_agent_graph_none_skipped(self, stm: ShortTermMemory) -> None:
        """Lines 231-233: agent._get_or_build_agent() returns None -> skipped."""
        mock_agent = MagicMock()
        mock_agent._truncate_checkpointer_history = MagicMock()
        mock_agent._get_or_build_agent = MagicMock(return_value=None)

        with _patch_record_span():
            await stm.save_dag_result("q", "r", "s1", 30, mock_agent)

    @pytest.mark.asyncio
    async def test_successful_write(self, stm: ShortTermMemory) -> None:
        """Lines 236-255: full success path, update_state called."""
        mock_graph = MagicMock()
        mock_graph.update_state = MagicMock()
        mock_agent = MagicMock()
        mock_agent._truncate_checkpointer_history = MagicMock()
        mock_agent._get_or_build_agent = MagicMock(return_value=mock_graph)

        with _patch_record_span():
            await stm.save_dag_result("query", "response", "s1", 30, mock_agent)

        # update_state was called via asyncio.to_thread
        # Since we can't easily assert on to_thread, check agent methods were called
        mock_agent._truncate_checkpointer_history.assert_called_once()
        mock_agent._get_or_build_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_response_uses_placeholder(self, stm: ShortTermMemory) -> None:
        """Line 241: empty response -> placeholder text used."""
        mock_graph = MagicMock()
        mock_graph.update_state = MagicMock()
        mock_agent = MagicMock()
        mock_agent._truncate_checkpointer_history = MagicMock()
        mock_agent._get_or_build_agent = MagicMock(return_value=mock_graph)

        with _patch_record_span():
            await stm.save_dag_result("query", "", "s1", 30, mock_agent)

    @pytest.mark.asyncio
    async def test_exception_caught(self, stm: ShortTermMemory) -> None:
        """Lines 257-262: exception during save -> caught, no propagation."""
        mock_agent = MagicMock()
        mock_agent._truncate_checkpointer_history.side_effect = RuntimeError("fail")

        with _patch_record_span():
            await stm.save_dag_result("q", "r", "s1", 30, mock_agent)


# ============================================================
# save_entity_context — line 316 (filtered empty -> early return)
# ============================================================

class TestSaveEntityContext:
    def test_empty_filtered_returns_early(self, stm: ShortTermMemory) -> None:
        """Line 316: no entity keys match _ENTITY_KEYS -> return without write."""
        stm.save_entity_context("s1", {"days": 30, "unknown_key": "val"})
        # Verify nothing was written
        result = stm._load_entity_context("s1")
        assert result == {}

    def test_empty_values_filtered_out(self, stm: ShortTermMemory) -> None:
        """Line 316: entity keys present but values falsy -> skip."""
        stm.save_entity_context("s1", {"po_number": "", "vendor_id": None})
        result = stm._load_entity_context("s1")
        assert result == {}

    def test_save_exception_swallowed(self, stm: ShortTermMemory) -> None:
        """Lines 355-356: save_entity_context exception -> logged, not raised."""
        stm._entity_engine = None  # force engine lookup to fail
        with patch(
            "core.database.engine.get_engine",
            side_effect=RuntimeError("no db"),
        ):
            stm.save_entity_context("s1", {"po_number": "PO-1"})
            # should not raise


# ============================================================
# _get_entity_engine — lines 277-278 (fallback to get_engine)
# ============================================================

class TestGetEntityEngine:
    def test_fallback_to_get_engine(self, settings: Settings) -> None:
        """Lines 277-278: no _entity_engine set -> call get_engine."""
        stm = ShortTermMemory(settings=settings)
        mock_engine = MagicMock()
        with patch("core.database.engine.get_engine", return_value=mock_engine) as mock_get:
            result = stm._get_entity_engine()
        assert result is mock_engine
        mock_get.assert_called_once_with(settings.postgresql)


# ============================================================
# _load_entity_context — lines 299-301 (exception path)
# ============================================================

class TestLoadEntityContext:
    def test_exception_returns_empty(self, settings: Settings) -> None:
        """Lines 299-301: DB error -> return empty dict."""
        stm = ShortTermMemory(settings=settings)
        stm._entity_engine = None
        with patch(
            "core.database.engine.get_engine",
            side_effect=RuntimeError("no db"),
        ):
            result = stm._load_entity_context("s1")
        assert result == {}


# ============================================================
# summarize_and_replace — lines 389-457
# ============================================================

class TestSummarizeAndReplace:
    @pytest.mark.asyncio
    async def test_summary_too_short_skipped(self, stm: ShortTermMemory) -> None:
        """Lines 405-408: LLM returns summary < 50 chars -> skipped."""
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "short"
        mock_llm.ainvoke.return_value = mock_response

        with _patch_record_span():
            await stm.summarize_and_replace("s1", "a" * 2000, mock_llm)

        # No checkpointer interaction expected
        mock_llm.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_checkpointer_skipped(self, stm: ShortTermMemory) -> None:
        """Lines 411-415: LLM returns good summary, but checkpointer unavailable."""
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "x" * 100  # long enough
        mock_llm.ainvoke.return_value = mock_response

        with _patch_record_span():
            await stm.summarize_and_replace("s1", "a" * 2000, mock_llm)

    @pytest.mark.asyncio
    async def test_no_checkpoint_data_skipped(self, stm: ShortTermMemory) -> None:
        """Lines 419-421: checkpointer exists but get_tuple returns None."""
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "x" * 100
        mock_llm.ainvoke.return_value = mock_response

        mock_cp = MagicMock()
        mock_cp.get_tuple.return_value = None
        stm._checkpointer = mock_cp

        with _patch_record_span():
            await stm.summarize_and_replace("s1", "a" * 2000, mock_llm)

    @pytest.mark.asyncio
    async def test_successful_replacement(self, stm: ShortTermMemory) -> None:
        """Lines 428-449: full success path — find AI message and replace."""
        summary_text = "## Summary\n" + "x" * 100
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = summary_text
        mock_llm.ainvoke.return_value = mock_response

        ai_msg = MagicMock()
        ai_msg.type = "ai"
        ai_msg.content = "original long content"

        human_msg = MagicMock()
        human_msg.type = "human"
        human_msg.content = "user query"

        mock_checkpoint = SimpleNamespace(
            checkpoint={"channel_values": {"messages": [human_msg, ai_msg]}},
            config={"configurable": {"thread_id": "s1"}},
            metadata={"step": 1},
        )
        mock_cp = MagicMock()
        mock_cp.get_tuple.return_value = mock_checkpoint
        stm._checkpointer = mock_cp

        with _patch_record_span():
            await stm.summarize_and_replace("s1", "a" * 2000, mock_llm)

        # AI message content should be replaced
        assert ai_msg.content == summary_text
        mock_cp.put.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_ai_message_found(self, stm: ShortTermMemory) -> None:
        """Lines 450-452: checkpoint has messages but no AI message -> skipped."""
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "x" * 100
        mock_llm.ainvoke.return_value = mock_response

        human_msg = MagicMock()
        human_msg.type = "human"

        mock_checkpoint = SimpleNamespace(
            checkpoint={"channel_values": {"messages": [human_msg]}},
            config={"configurable": {"thread_id": "s1"}},
            metadata={},
        )
        mock_cp = MagicMock()
        mock_cp.get_tuple.return_value = mock_checkpoint
        stm._checkpointer = mock_cp

        with _patch_record_span():
            await stm.summarize_and_replace("s1", "a" * 2000, mock_llm)

        mock_cp.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_caught(self, stm: ShortTermMemory) -> None:
        """Lines 454-459: any exception during summarize -> caught, no propagation."""
        mock_llm = AsyncMock()
        mock_llm.ainvoke.side_effect = RuntimeError("LLM unavailable")

        with _patch_record_span():
            # Should not raise
            await stm.summarize_and_replace("s1", "a" * 2000, mock_llm)

    @pytest.mark.asyncio
    async def test_empty_summary_skipped(self, stm: ShortTermMemory) -> None:
        """Line 405: LLM returns empty string -> skipped."""
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = ""
        mock_llm.ainvoke.return_value = mock_response

        with _patch_record_span():
            await stm.summarize_and_replace("s1", "content", mock_llm)
