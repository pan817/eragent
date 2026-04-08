"""LongTermMemory 单元测试（使用 SQLite 内存数据库替代 PostgreSQL）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa

from core.memory import LongTermMemory, metadata_obj, ShortTermMemory


@pytest.fixture()
def ltm() -> LongTermMemory:
    """创建使用 SQLite 内存数据库的 LongTermMemory。"""
    # 使用 SQLite 内存数据库避免依赖 PostgreSQL
    memory = LongTermMemory.__new__(LongTermMemory)
    memory.dsn = "sqlite://"
    memory.engine = sa.create_engine("sqlite://")
    metadata_obj.create_all(memory.engine)
    return memory


class TestLongTermMemoryInit:
    """初始化测试。"""

    def test_init_tables(self, ltm: LongTermMemory) -> None:
        """init_tables 应幂等创建表。"""
        ltm.init_tables()  # 不应抛异常


class TestMemoryOperations:
    """记忆 CRUD 测试。"""

    def test_save_memory(self, ltm: LongTermMemory) -> None:
        """保存记忆应返回 UUID。"""
        memory_id = ltm.save_memory(
            user_id="user1",
            session_id="sess1",
            memory_type="conversation",
            content="测试记忆内容",
            metadata={"key": "value"},
        )
        assert memory_id
        assert len(memory_id) == 36  # UUID 格式

    def test_search_memories(self, ltm: LongTermMemory) -> None:
        """搜索记忆应返回匹配结果。"""
        ltm.save_memory("user1", "sess1", "insight", "采购异常分析", {})
        ltm.save_memory("user1", "sess1", "insight", "库存周转分析", {})

        results = ltm.search_memories("user1", "采购")
        assert len(results) == 1
        assert "采购" in results[0]["content"]

    def test_search_memories_no_match(self, ltm: LongTermMemory) -> None:
        """无匹配时应返回空列表。"""
        results = ltm.search_memories("user1", "不存在的关键词")
        assert results == []

    def test_search_memories_limit(self, ltm: LongTermMemory) -> None:
        """limit 参数应限制返回数量。"""
        for i in range(5):
            ltm.save_memory("user1", "sess1", "insight", f"记忆 {i}", {})
        results = ltm.search_memories("user1", "记忆", limit=3)
        assert len(results) == 3


class TestReportOperations:
    """报告 CRUD 测试。"""

    def test_save_and_get_report(self, ltm: LongTermMemory) -> None:
        """保存报告后应能按 ID 获取。"""
        report_id = ltm.save_report(
            user_id="user1",
            session_id="sess1",
            query="分析三路匹配",
            analysis_type="three_way_match",
            result_json='{"anomalies": []}',
            report_markdown="# 报告",
            anomaly_count=0,
        )
        report = ltm.get_report(report_id)
        assert report is not None
        assert report["query"] == "分析三路匹配"
        assert report["anomaly_count"] == 0

    def test_get_report_not_found(self, ltm: LongTermMemory) -> None:
        """不存在的报告应返回 None。"""
        report = ltm.get_report("nonexistent-id")
        assert report is None

    def test_list_reports(self, ltm: LongTermMemory) -> None:
        """列出用户报告应按创建时间倒序。"""
        ltm.save_report("user1", "s1", "q1", "type1", "{}", "", 0)
        ltm.save_report("user1", "s2", "q2", "type2", "{}", "", 1)
        ltm.save_report("user2", "s3", "q3", "type3", "{}", "", 2)

        reports = ltm.list_reports("user1")
        assert len(reports) == 2
        # 最新的在前
        assert reports[0]["query"] == "q2"

    def test_list_reports_limit(self, ltm: LongTermMemory) -> None:
        """limit 参数应限制返回数量。"""
        for i in range(5):
            ltm.save_report("user1", f"s{i}", f"q{i}", "type", "{}", "", i)
        reports = ltm.list_reports("user1", limit=2)
        assert len(reports) == 2

    def test_list_reports_empty(self, ltm: LongTermMemory) -> None:
        """无报告时应返回空列表。"""
        reports = ltm.list_reports("user_no_reports")
        assert reports == []


class TestLongTermMemoryWithVectorStore:
    """LongTermMemory 接入向量库的旁路测试（使用 MagicMock vector store）。"""

    @pytest.fixture()
    def ltm_with_vs(self) -> tuple[LongTermMemory, MagicMock]:
        vs = MagicMock()
        memory = LongTermMemory.__new__(LongTermMemory)
        memory.dsn = "sqlite://"
        memory.engine = sa.create_engine("sqlite://")
        memory._vector_store = vs
        metadata_obj.create_all(memory.engine)
        return memory, vs

    def test_save_memory_mirrors_to_vector(self, ltm_with_vs: tuple[LongTermMemory, MagicMock]) -> None:
        ltm, vs = ltm_with_vs
        mid = ltm.save_memory("u1", "s1", "insight", "测试内容", {})
        vs.add_documents.assert_called_once()
        docs = vs.add_documents.call_args.args[0]
        assert docs[0]["id"] == f"memory_{mid}"
        assert docs[0]["metadata"]["user_id"] == "u1"
        assert docs[0]["metadata"]["source"] == "memory"

    def test_save_memory_vector_failure_swallowed(
        self, ltm_with_vs: tuple[LongTermMemory, MagicMock]
    ) -> None:
        ltm, vs = ltm_with_vs
        vs.add_documents.side_effect = RuntimeError("vector down")
        # 不应抛异常
        mid = ltm.save_memory("u1", "s1", "t", "c", {})
        assert mid

    def test_search_memories_semantic(self, ltm_with_vs: tuple[LongTermMemory, MagicMock]) -> None:
        ltm, vs = ltm_with_vs
        vs.search.return_value = [{"id": "memory_1", "text": "x", "metadata": {}, "distance": 0.1}]
        results = ltm.search_memories_semantic("u1", "查询", limit=3)
        assert len(results) == 1
        kwargs = vs.search.call_args.kwargs
        assert kwargs["where"] == {"user_id": "u1", "source": "memory"}
        assert kwargs["top_k"] == 3

    def test_search_memories_semantic_no_vector(self, ltm: LongTermMemory) -> None:
        # 未注入向量库时返回空
        assert ltm.search_memories_semantic("u1", "q") == []

    def test_search_memories_semantic_swallows_error(
        self, ltm_with_vs: tuple[LongTermMemory, MagicMock]
    ) -> None:
        ltm, vs = ltm_with_vs
        vs.search.side_effect = RuntimeError("boom")
        assert ltm.search_memories_semantic("u1", "q") == []

    def test_save_report_mirrors_to_vector(self, ltm_with_vs: tuple[LongTermMemory, MagicMock]) -> None:
        ltm, vs = ltm_with_vs
        rid = ltm.save_report("u1", "s1", "q", "t", "{}", "# 报告内容", 2)
        vs.add_documents.assert_called_once()
        docs = vs.add_documents.call_args.args[0]
        assert docs[0]["id"] == f"report_{rid}"
        assert docs[0]["metadata"]["source"] == "report"
        assert docs[0]["metadata"]["anomaly_count"] == 2

    def test_search_reports_semantic(self, ltm_with_vs: tuple[LongTermMemory, MagicMock]) -> None:
        ltm, vs = ltm_with_vs
        vs.search.return_value = [{"id": "report_1", "text": "x", "metadata": {}, "distance": 0.1}]
        results = ltm.search_reports_semantic("u1", "三路匹配")
        assert len(results) == 1
        kwargs = vs.search.call_args.kwargs
        assert kwargs["where"] == {"user_id": "u1", "source": "report"}

    def test_search_reports_semantic_no_vector(self, ltm: LongTermMemory) -> None:
        assert ltm.search_reports_semantic("u1", "q") == []

    def test_delete_user_data_clears_vector(
        self, ltm_with_vs: tuple[LongTermMemory, MagicMock]
    ) -> None:
        ltm, vs = ltm_with_vs
        ltm.save_memory("u1", "s1", "t", "c", {})
        ltm.delete_user_data("u1")
        vs.delete.assert_called_once_with(where={"user_id": "u1"})

    def test_delete_user_data_swallows_vector_error(
        self, ltm_with_vs: tuple[LongTermMemory, MagicMock]
    ) -> None:
        ltm, vs = ltm_with_vs
        vs.delete.side_effect = RuntimeError("boom")
        # 不应抛异常
        ltm.delete_user_data("u1")


class TestShortTermMemoryExtra:
    """ShortTermMemory 额外覆盖测试。"""

    def test_get_context_with_summary(self) -> None:
        """有摘要时 get_context 应包含 system 消息。"""
        stm = ShortTermMemory()
        stm.summary = "早期对话摘要"
        stm.add_message("user", "最新消息")
        ctx = stm.get_context()
        assert ctx[0]["role"] == "system"
        assert "摘要" in ctx[0]["content"]
        assert ctx[1]["content"] == "最新消息"

    def test_compress_with_existing_summary(self) -> None:
        """已有摘要时压缩应合并。"""
        stm = ShortTermMemory(max_messages=20, summary_threshold=8)
        stm.summary = "旧摘要"
        for i in range(12):
            stm.add_message("user", f"消息 {i}")

        stm.compress(lambda msgs: "新摘要")
        assert "旧摘要" in stm.summary
        assert "新摘要" in stm.summary

    def test_compress_keep_count_exceeds_messages(self) -> None:
        """消息数不足时压缩应直接返回。"""
        stm = ShortTermMemory(max_messages=20, summary_threshold=3)
        stm.add_message("user", "m1")
        stm.add_message("user", "m2")
        stm.add_message("user", "m3")
        stm.compress(lambda msgs: "summary")
        # keep_count = max(3//2, 5) = 5 >= 3, so no compression
        assert stm.summary == ""
        assert len(stm.messages) == 3
