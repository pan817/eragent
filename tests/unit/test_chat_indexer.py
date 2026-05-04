"""ChatHistoryIndexer (core/memory/chat_indexer.py) 单元测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import Settings
from core.memory.chat_indexer import ChatHistoryIndexer, _Fragment, _PendingMessage
from core.time_utils import now_cn


@pytest.fixture
def settings() -> Settings:
    s = Settings()
    s.memory.chat_history.indexing_enabled = True
    s.memory.chat_history.fragment_aggregation_seconds = 1
    s.memory.chat_history.fragment_max_messages = 6
    return s


@pytest.fixture
def indexer(settings: Settings) -> ChatHistoryIndexer:
    return ChatHistoryIndexer(settings)


class TestEnqueue:
    def test_enqueue_adds_to_queue(self, indexer: ChatHistoryIndexer) -> None:
        indexer.enqueue("s1", "m1", "user", "hello", "u1")
        assert len(indexer._queue) == 1

    def test_enqueue_skipped_when_disabled(self, settings: Settings) -> None:
        settings.memory.chat_history.indexing_enabled = False
        idx = ChatHistoryIndexer(settings)
        idx.enqueue("s1", "m1", "user", "hello", "u1")
        assert len(idx._queue) == 0

    def test_enqueue_skipped_when_closed(self, indexer: ChatHistoryIndexer) -> None:
        indexer.close()
        indexer.enqueue("s1", "m1", "user", "hello", "u1")
        assert len(indexer._queue) == 0

    def test_multiple_enqueue(self, indexer: ChatHistoryIndexer) -> None:
        for i in range(5):
            indexer.enqueue("s1", f"m{i}", "user" if i % 2 == 0 else "assistant", f"msg{i}", "u1")
        assert len(indexer._queue) == 5


class TestAggregateFragments:
    def test_user_assistant_pair(self, indexer: ChatHistoryIndexer) -> None:
        now = now_cn()
        msgs = [
            _PendingMessage("s1", "m1", "user", "hello", "u1", now),
            _PendingMessage("s1", "m2", "assistant", "hi", "u1", now),
        ]
        fragments = indexer._aggregate_fragments(msgs)
        assert len(fragments) == 1
        assert fragments[0].role_pattern == "user→assistant"
        assert "m1" in fragments[0].message_ids
        assert "m2" in fragments[0].message_ids

    def test_multiple_pairs_same_session(self, indexer: ChatHistoryIndexer) -> None:
        now = now_cn()
        msgs = [
            _PendingMessage("s1", "m1", "user", "q1", "u1", now),
            _PendingMessage("s1", "m2", "assistant", "a1", "u1", now),
            _PendingMessage("s1", "m3", "user", "q2", "u1", now),
            _PendingMessage("s1", "m4", "assistant", "a2", "u1", now),
        ]
        fragments = indexer._aggregate_fragments(msgs)
        assert len(fragments) == 2

    def test_cross_session_separated(self, indexer: ChatHistoryIndexer) -> None:
        now = now_cn()
        msgs = [
            _PendingMessage("s1", "m1", "user", "q1", "u1", now),
            _PendingMessage("s1", "m2", "assistant", "a1", "u1", now),
            _PendingMessage("s2", "m3", "user", "q2", "u1", now),
        ]
        fragments = indexer._aggregate_fragments(msgs)
        assert len(fragments) == 2
        session_ids = {f.session_id for f in fragments}
        assert session_ids == {"s1", "s2"}

    def test_max_messages_cap(self, indexer: ChatHistoryIndexer) -> None:
        now = now_cn()
        msgs = [
            _PendingMessage("s1", f"m{i}", "user", f"msg{i}", "u1", now)
            for i in range(10)
        ]
        fragments = indexer._aggregate_fragments(msgs)
        for f in fragments:
            assert len(f.message_ids) <= 6

    def test_trailing_chunk(self, indexer: ChatHistoryIndexer) -> None:
        now = now_cn()
        msgs = [
            _PendingMessage("s1", "m1", "user", "q1", "u1", now),
        ]
        fragments = indexer._aggregate_fragments(msgs)
        assert len(fragments) == 1


class TestBuildFragment:
    def test_content_format(self) -> None:
        now = now_cn()
        chunk = [
            _PendingMessage("s1", "m1", "user", "hello", "u1", now),
            _PendingMessage("s1", "m2", "assistant", "world", "u1", now),
        ]
        f = ChatHistoryIndexer._build_fragment("s1", chunk)
        assert "[user] hello" in f.content
        assert "[assistant] world" in f.content
        assert f.session_id == "s1"
        assert f.user_id == "u1"


class TestFlush:
    @pytest.mark.asyncio
    async def test_flush_empty_queue(self, indexer: ChatHistoryIndexer) -> None:
        count = await indexer.flush()
        assert count == 0

    @pytest.mark.asyncio
    async def test_flush_writes_to_chroma(self, indexer: ChatHistoryIndexer) -> None:
        mock_collection = MagicMock()
        mock_store = MagicMock()
        mock_store._ensure_collection.return_value = mock_collection
        indexer._vector_store = mock_store

        indexer.enqueue("s1", "m1", "user", "hello", "u1")
        indexer.enqueue("s1", "m2", "assistant", "hi", "u1")

        count = await indexer.flush()
        assert count == 1
        mock_collection.upsert.assert_called_once()
        call_kwargs = mock_collection.upsert.call_args
        assert "chat_s1_m1" in call_kwargs.kwargs.get("ids", call_kwargs[1].get("ids", []))

    @pytest.mark.asyncio
    async def test_flush_clears_queue(self, indexer: ChatHistoryIndexer) -> None:
        mock_store = MagicMock()
        mock_store._ensure_collection.return_value = MagicMock()
        indexer._vector_store = mock_store

        indexer.enqueue("s1", "m1", "user", "test", "u1")
        await indexer.flush()
        assert len(indexer._queue) == 0


class TestDeadLetter:
    @pytest.mark.asyncio
    async def test_write_dead_letter_on_chroma_failure(self, indexer: ChatHistoryIndexer) -> None:
        mock_store = MagicMock()
        mock_store._ensure_collection.return_value = MagicMock(
            upsert=MagicMock(side_effect=RuntimeError("chroma down")),
        )
        indexer._vector_store = mock_store

        with patch("core.memory.chat_indexer.ChatHistoryIndexer._write_dead_letter", new_callable=AsyncMock) as mock_dl:
            indexer.enqueue("s1", "m1", "user", "hello", "u1")
            await indexer.flush()
            mock_dl.assert_called_once()

    @pytest.mark.asyncio
    async def test_dead_letter_on_no_vector_store(self, indexer: ChatHistoryIndexer) -> None:
        indexer._vector_store = None
        with patch.object(indexer, "_get_vector_store", return_value=None):
            with patch.object(indexer, "_write_dead_letter", new_callable=AsyncMock) as mock_dl:
                indexer.enqueue("s1", "m1", "user", "hello", "u1")
                await indexer.flush()
                mock_dl.assert_called_once()


class TestClose:
    def test_close_prevents_enqueue(self, indexer: ChatHistoryIndexer) -> None:
        indexer.close()
        indexer.enqueue("s1", "m1", "user", "hello", "u1")
        assert len(indexer._queue) == 0

    def test_close_cancels_flush_task(self, indexer: ChatHistoryIndexer) -> None:
        mock_task = MagicMock()
        mock_task.done.return_value = False
        indexer._flush_task = mock_task
        indexer.close()
        mock_task.cancel.assert_called_once()


class TestVectorStoreInit:
    def test_lazy_init_returns_none_on_failure(self, indexer: ChatHistoryIndexer) -> None:
        with patch("core.knowledge.vector_store.VectorStore.from_settings", side_effect=RuntimeError("no chroma")):
            result = indexer._get_vector_store()
            assert result is None
