"""search_my_chat_history 工具 + search_chat_history 检索单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import Settings
from core.memory.manager import MemoryManager
from core.time_utils import now_cn


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def manager(settings: Settings) -> MemoryManager:
    with patch("core.memory.short_term.ShortTermMemory"):
        return MemoryManager(settings=settings)


# ── search_chat_history 方法测试 ──────────────────────────────


class TestSearchChatHistory:
    @pytest.mark.asyncio
    async def test_returns_empty_on_timeout(self, manager: MemoryManager) -> None:
        manager._settings.memory.chat_history.search_timeout_seconds = 0.001

        async def slow_impl(*args, **kwargs):
            await asyncio.sleep(1)
            return []

        with patch.object(manager, "_search_chat_history_impl", slow_impl):
            result = await manager.search_chat_history("u1", "test")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self, manager: MemoryManager) -> None:
        with patch.object(
            manager, "_search_chat_history_impl",
            side_effect=RuntimeError("db down"),
        ):
            result = await manager.search_chat_history("u1", "test")
        assert result == []


class TestChannelAExact:
    def test_returns_empty_when_engine_unavailable(self, manager: MemoryManager) -> None:
        with patch("core.database.engine.get_engine", side_effect=RuntimeError("no db")):
            result = manager._channel_a_exact("u1", ["SUP-001"], None, 5)
        assert result == []


class TestChannelBSemantic:
    def test_returns_empty_when_no_indexer(self, manager: MemoryManager) -> None:
        with patch.object(manager, "_get_chat_indexer", return_value=None):
            result = manager._channel_b_semantic("u1", "test query", None, 5)
        assert result == []

    def test_returns_empty_when_no_vector_store(self, manager: MemoryManager) -> None:
        mock_indexer = MagicMock()
        mock_indexer._get_vector_store.return_value = None
        with patch.object(manager, "_get_chat_indexer", return_value=mock_indexer):
            result = manager._channel_b_semantic("u1", "test query", None, 5)
        assert result == []

    def test_converts_chroma_hits(self, manager: MemoryManager) -> None:
        mock_store = MagicMock()
        mock_store.search.return_value = [
            {
                "id": "doc1",
                "text": "hello world " * 50,
                "metadata": {
                    "session_id": "s1",
                    "user_id": "u1",
                    "created_at": now_cn().isoformat(),
                },
                "distance": 0.3,
            }
        ]
        mock_indexer = MagicMock()
        mock_indexer._get_vector_store.return_value = mock_store
        with patch.object(manager, "_get_chat_indexer", return_value=mock_indexer):
            result = manager._channel_b_semantic("u1", "hello", None, 5)
        assert len(result) == 1
        assert result[0]["session_id"] == "s1"
        assert result[0]["base_score"] == pytest.approx(0.7, abs=0.01)
        assert len(result[0]["snippet"]) <= 200


class TestSearchChatHistoryImpl:
    @pytest.mark.asyncio
    async def test_dual_channel_dedup(self, manager: MemoryManager) -> None:
        now = now_cn()
        exact_hit = {
            "session_id": "s1", "session_title": "T1", "snippet": "sn1",
            "entities": {"vendor_id": "SUP-001"}, "created_at": now,
        }
        semantic_hit_dup = {
            "session_id": "s1", "session_title": "", "snippet": "sn2",
            "entities": {}, "created_at": now.isoformat(), "base_score": 0.8,
        }
        semantic_hit_new = {
            "session_id": "s2", "session_title": "", "snippet": "sn3",
            "entities": {}, "created_at": now.isoformat(), "base_score": 0.7,
        }
        with (
            patch.object(manager, "_channel_a_exact", return_value=[exact_hit]),
            patch.object(manager, "_channel_b_semantic", return_value=[semantic_hit_dup, semantic_hit_new]),
        ):
            results = await manager._search_chat_history_impl(
                "u1", "test", ["SUP-001"], 30, 5,
            )
        session_ids = [r["session_id"] for r in results]
        assert "s1" in session_ids
        assert "s2" in session_ids
        assert session_ids.count("s1") == 1

    @pytest.mark.asyncio
    async def test_recency_decay_applied(self, manager: MemoryManager) -> None:
        now = now_cn()
        old_hit = {
            "session_id": "s_old", "session_title": "", "snippet": "",
            "entities": {}, "created_at": now - timedelta(days=60), "base_score": 1.0,
        }
        new_hit = {
            "session_id": "s_new", "session_title": "", "snippet": "",
            "entities": {}, "created_at": now - timedelta(days=1), "base_score": 1.0,
        }
        with (
            patch.object(manager, "_channel_a_exact", return_value=[]),
            patch.object(manager, "_channel_b_semantic", return_value=[old_hit, new_hit]),
        ):
            results = await manager._search_chat_history_impl(
                "u1", "test", None, 90, 5,
            )
        assert results[0]["session_id"] == "s_new"
        assert results[0]["recency_score"] > results[1]["recency_score"]


# ── @tool search_my_chat_history 测试 ─────────────────────────


class TestSearchMyToolInvocation:
    @pytest.mark.asyncio
    async def test_returns_empty_without_user_id(self) -> None:
        from modules.p2p.tools.chat_history import search_my_chat_history

        with patch("core.tasks.context.get_current_user_id", return_value=None):
            result = await search_my_chat_history.ainvoke({"query": "test"})
        assert result == "[]"

    @pytest.mark.asyncio
    async def test_clamps_parameters(self) -> None:
        from modules.p2p.tools.chat_history import search_my_chat_history

        with (
            patch("core.tasks.context.get_current_user_id", return_value="u1"),
            patch("core.memory.manager.MemoryManager.search_chat_history", new_callable=AsyncMock, return_value=[]) as mock_search,
        ):
            result = await search_my_chat_history.ainvoke({"query": "test", "days": 999, "limit": 50})
        call_kwargs = mock_search.call_args.kwargs
        assert call_kwargs["days"] <= 365
        assert call_kwargs["limit"] <= 10


# ── Golden RECALL 用例（10 条）────────────────────────────────


class TestGoldenRecallCases:
    """10 条 golden 用例验证 RECALL 路由识别。

    这些用例验证 unified_router 的 prompt 能正确将跨会话引用识别为 recall。
    由于实际 LLM 调用在集成测试中验证，这里仅验证正则 fast-path 不再误判。
    """

    @pytest.fixture
    def queries_and_expected(self) -> list[tuple[str, str]]:
        return [
            ("上周我们讨论的那家供应商，本月还有异常发票吗？", "recall"),
            ("继续上个月那个三路匹配差异分析", "recall"),
            ("我之前说过的那个付款政策，再帮我查一遍", "recall"),
            ("上次分析的结果呢", "recall"),
            ("那家化工厂后来怎么样了", "recall"),
            ("之前讨论的SUP-003的问题解决了吗", "recall"),
            ("我们上回分析的供应商绩效，数据更新了吗", "recall"),
            ("上个月在采购合规上得出过什么结论", "recall"),
            ("帮我回顾一下之前的付款违规分析", "recall"),
            ("还记得我上次让你查的那批发票吗", "recall"),
        ]

    def test_recall_queries_not_bypassed_as_analysis(
        self, queries_and_expected: list[tuple[str, str]],
    ) -> None:
        from core.orchestrator.router import _classify_bypass

        for query, expected_kind in queries_and_expected:
            result = _classify_bypass(query)
            if result is not None:
                assert result.value in ("recall", "meta", "chitchat"), (
                    f"Query '{query}' was classified as '{result.value}' "
                    f"by bypass, expected 'recall' or None (defer to LLM)"
                )

    def test_recall_intent_kind_in_valid_set(self) -> None:
        from core.orchestrator.unified_router import _VALID_INTENT_KINDS

        assert "recall" in _VALID_INTENT_KINDS
