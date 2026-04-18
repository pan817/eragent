"""LongTermMemory 单元测试（使用 SQLite 内存数据库替代 PostgreSQL）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa

from core.memory import LongTermMemory, MemoryRepository, ReportRepository
from core.memory.tables import memories_table, metadata_obj
from core.memory.long_term import _VectorStoreProxy, _content_hash, _normalize_content, _rrf_fuse, _to_tsquery_or


def _make_ltm_base(
    *,
    max_per_user: int = 0,
    min_content_len: int = 0,
    dedupe_window_seconds: int = 0,
    skip_empty_conclusions: bool = False,
    vector_store: object = None,
) -> LongTermMemory:
    """构造 SQLite 内存版 LongTermMemory，供各测试 class 复用。"""
    engine = sa.create_engine("sqlite://")
    metadata_obj.create_all(engine)
    return LongTermMemory(
        engine=engine,
        vector_store=vector_store,
        max_per_user=max_per_user,
        min_content_len=min_content_len,
        dedupe_window_seconds=dedupe_window_seconds,
        skip_empty_conclusions=skip_empty_conclusions,
    )


@pytest.fixture()
def ltm() -> LongTermMemory:
    """创建使用 SQLite 内存数据库的 LongTermMemory。"""
    return _make_ltm_base()


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
        memory = _make_ltm_base(vector_store=vs)
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

    def test_search_memories_hybrid_uses_vector_when_like_misses(
        self, ltm_with_vs: tuple[LongTermMemory, MagicMock]
    ) -> None:
        """LIKE 路径未命中时,向量路径召回的 id 仍应出现在 hybrid 结果中。"""
        ltm, vs = ltm_with_vs
        # 写入一条 content 与 query 字面无重叠的记忆
        mid = ltm.save_memory("u1", "s1", "insight", "三路匹配异常分析", {})
        # 向量路径返回该 id（带 memory_ 前缀,模拟 vector_store.search 输出）
        vs.search.return_value = [
            {"id": f"memory_{mid}", "text": "x", "metadata": {"user_id": "u1"}, "distance": 0.1}
        ]
        # query 字面与 content 无重叠 —— LIKE 路径必失败
        results = ltm.search_memories("u1", "supplier risk", limit=5)
        assert len(results) == 1
        assert results[0]["id"] == mid

    def test_search_memories_hybrid_fuses_both_paths(
        self, ltm_with_vs: tuple[LongTermMemory, MagicMock]
    ) -> None:
        """稀疏与稠密两路都有命中时,RRF 融合后两条都应返回。"""
        ltm, vs = ltm_with_vs
        mid_sparse = ltm.save_memory("u1", "s1", "insight", "采购订单异常", {})
        mid_dense = ltm.save_memory("u1", "s1", "insight", "供应商绩效", {})
        vs.search.return_value = [
            {"id": f"memory_{mid_dense}", "text": "x", "metadata": {"user_id": "u1"}, "distance": 0.1}
        ]
        results = ltm.search_memories("u1", "采购", limit=5)
        ids = {r["id"] for r in results}
        assert mid_sparse in ids
        assert mid_dense in ids


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
        # MemoryRepository 和 ReportRepository 各自调一次 delete
        assert vs.delete.call_count == 2

    def test_delete_user_data_swallows_vector_error(
        self, ltm_with_vs: tuple[LongTermMemory, MagicMock]
    ) -> None:
        ltm, vs = ltm_with_vs
        vs.delete.side_effect = RuntimeError("boom")
        # 不应抛异常
        ltm.delete_user_data("u1")


class TestUserCap:
    """每个 user_id 的滚动 cap 测试。"""

    def _make_ltm(self, max_per_user: int, vs: MagicMock | None = None) -> LongTermMemory:
        return _make_ltm_base(max_per_user=max_per_user, vector_store=vs)

    def test_save_memory_enforces_cap(self) -> None:
        ltm = self._make_ltm(max_per_user=3)
        for i in range(5):
            ltm.save_memory("u1", "s1", "insight", f"记忆{i}", {})
        with ltm._mem_repo._engine.connect() as conn:
            rows = conn.execute(
                sa.select(memories_table.c.content)
                .where(memories_table.c.user_id == "u1")
                .order_by(memories_table.c.created_at.desc())
            ).fetchall()
        assert len(rows) == 3
        # 最新的三条应保留
        contents = [r[0] for r in rows]
        assert "记忆4" in contents
        assert "记忆3" in contents
        assert "记忆2" in contents

    def test_cap_zero_means_unlimited(self) -> None:
        ltm = self._make_ltm(max_per_user=0)
        for i in range(10):
            ltm.save_memory("u1", "s1", "insight", f"记忆{i}", {})
        with ltm._mem_repo._engine.connect() as conn:
            count = conn.execute(
                sa.select(sa.func.count()).select_from(memories_table)
            ).scalar()
        assert count == 10

    def test_cap_isolated_per_user(self) -> None:
        ltm = self._make_ltm(max_per_user=3)
        for i in range(5):
            ltm.save_memory("u1", "s1", "insight", f"u1-{i}", {})
        for i in range(5):
            ltm.save_memory("u2", "s2", "insight", f"u2-{i}", {})
        with ltm._mem_repo._engine.connect() as conn:
            u1_count = conn.execute(
                sa.select(sa.func.count())
                .select_from(memories_table)
                .where(memories_table.c.user_id == "u1")
            ).scalar()
            u2_count = conn.execute(
                sa.select(sa.func.count())
                .select_from(memories_table)
                .where(memories_table.c.user_id == "u2")
            ).scalar()
        assert u1_count == 3
        assert u2_count == 3

    def test_cap_deletes_vector_documents(self) -> None:
        vs = MagicMock()
        ltm = self._make_ltm(max_per_user=2, vs=vs)
        ids = [
            ltm.save_memory("u1", "s1", "insight", f"记忆{i}", {})
            for i in range(4)
        ]
        # 前两条应被淘汰,且向量库 delete 应被调用并包含这两个 id
        all_delete_calls = [c for c in vs.delete.call_args_list]
        assert all_delete_calls, "vector_store.delete 应至少被调用一次"
        deleted_ids: set[str] = set()
        for call in all_delete_calls:
            for arg_id in call.kwargs.get("ids", []):
                deleted_ids.add(arg_id)
        assert f"memory_{ids[0]}" in deleted_ids
        assert f"memory_{ids[1]}" in deleted_ids

    def test_cap_swallows_vector_delete_error(self) -> None:
        vs = MagicMock()
        vs.delete.side_effect = RuntimeError("vector down")
        ltm = self._make_ltm(max_per_user=2, vs=vs)
        # 不应抛异常
        for i in range(4):
            ltm.save_memory("u1", "s1", "insight", f"x{i}", {})
        with ltm._mem_repo._engine.connect() as conn:
            count = conn.execute(
                sa.select(sa.func.count()).select_from(memories_table)
            ).scalar()
        assert count == 2


class TestWriteFilters:
    """三层写入过滤策略（L1 长度 / L2 去重 / L3 空结论）单元测试。"""

    # ---- L1: 内容长度过滤 ----

    def test_save_skipped_too_short(self) -> None:
        ltm = _make_ltm_base(min_content_len=10)
        result = ltm.save_memory("u1", "s1", "t", "短", {})
        assert result is None
        with ltm._mem_repo._engine.connect() as conn:
            count = conn.execute(
                sa.select(sa.func.count()).select_from(memories_table)
            ).scalar()
        assert count == 0

    def test_save_skipped_too_short_exact_boundary(self) -> None:
        """长度 == threshold - 1 应跳过；长度 == threshold 应写入。"""
        ltm = _make_ltm_base(min_content_len=5)
        assert ltm.save_memory("u1", "s1", "t", "1234", {}) is None  # len=4 < 5
        mid = ltm.save_memory("u1", "s1", "t", "12345", {})           # len=5 == 5
        assert mid is not None

    def test_save_min_content_len_disabled(self) -> None:
        """min_content_len=0 时短内容也写入。"""
        ltm = _make_ltm_base(min_content_len=0)
        result = ltm.save_memory("u1", "s1", "t", "x", {})
        assert result is not None

    # ---- L2: 内容指纹去重 ----

    def test_save_dedupe_within_window(self) -> None:
        ltm = _make_ltm_base(dedupe_window_seconds=900)
        content = "同一用户的相同内容应被去重"
        mid1 = ltm.save_memory("u1", "s1", "t", content, {})
        mid2 = ltm.save_memory("u1", "s1", "t", content, {})
        assert mid1 is not None
        assert mid2 is None
        with ltm._mem_repo._engine.connect() as conn:
            count = conn.execute(
                sa.select(sa.func.count()).select_from(memories_table)
            ).scalar()
        assert count == 1

    def test_save_dedupe_normalizes_whitespace(self) -> None:
        """仅空白 / 大小写差异的内容应被识别为重复。"""
        ltm = _make_ltm_base(dedupe_window_seconds=900)
        mid1 = ltm.save_memory("u1", "s1", "t", "Hello  World", {})
        mid2 = ltm.save_memory("u1", "s1", "t", "hello world", {})
        assert mid1 is not None
        assert mid2 is None

    def test_save_dedupe_isolated_per_user(self) -> None:
        """同内容不同 user 互不影响。"""
        ltm = _make_ltm_base(dedupe_window_seconds=900)
        content = "相同内容"
        assert ltm.save_memory("u1", "s1", "t", content, {}) is not None
        assert ltm.save_memory("u2", "s2", "t", content, {}) is not None

    def test_save_dedupe_disabled(self) -> None:
        """dedupe_window_seconds=0 时不去重。"""
        ltm = _make_ltm_base(dedupe_window_seconds=0)
        content = "重复内容"
        assert ltm.save_memory("u1", "s1", "t", content, {}) is not None
        assert ltm.save_memory("u1", "s1", "t", content, {}) is not None

    # ---- L3: 空结论过滤 ----

    def test_save_skip_empty_conclusion(self) -> None:
        ltm = _make_ltm_base(skip_empty_conclusions=True)
        result = ltm.save_memory(
            "u1", "s1", "t", "分析完成",
            {"anomaly_count": 0, "summary": ""},
        )
        assert result is None

    def test_save_skip_empty_conclusion_with_anomaly(self) -> None:
        """anomaly_count > 0 时不跳过。"""
        ltm = _make_ltm_base(skip_empty_conclusions=True)
        result = ltm.save_memory(
            "u1", "s1", "t", "发现3个异常",
            {"anomaly_count": 3, "summary": ""},
        )
        assert result is not None

    def test_save_skip_empty_conclusion_with_summary(self) -> None:
        """有 summary 时不跳过（即使 anomaly_count=0）。"""
        ltm = _make_ltm_base(skip_empty_conclusions=True)
        result = ltm.save_memory(
            "u1", "s1", "t", "整体正常",
            {"anomaly_count": 0, "summary": "供应链运转正常"},
        )
        assert result is not None

    def test_save_skip_empty_conclusion_disabled_by_default(self) -> None:
        """skip_empty_conclusions 默认 False，无结论也写入。"""
        ltm = _make_ltm_base(skip_empty_conclusions=False)
        result = ltm.save_memory(
            "u1", "s1", "t", "分析完成",
            {"anomaly_count": 0, "summary": ""},
        )
        assert result is not None

    # ---- content_hash 写入数据库 ----

    def test_content_hash_written_to_db(self) -> None:
        """save_memory 应把 content_hash 写入 DB。"""
        ltm = _make_ltm_base()
        content = "需要计算 hash 的内容"
        mid = ltm.save_memory("u1", "s1", "t", content, {})
        assert mid is not None
        with ltm._mem_repo._engine.connect() as conn:
            row = conn.execute(
                sa.select(memories_table.c.content_hash).where(
                    memories_table.c.id == mid
                )
            ).fetchone()
        assert row is not None
        assert row[0] == _content_hash(content)

    # ---- 返回值语义 ----

    def test_save_returns_none_on_skip(self) -> None:
        ltm = _make_ltm_base(min_content_len=100)
        assert ltm.save_memory("u1", "s1", "t", "短内容", {}) is None

    def test_save_returns_uuid_on_write(self) -> None:
        ltm = _make_ltm_base()
        mid = ltm.save_memory("u1", "s1", "t", "正常内容长度足够写入", {})
        assert mid is not None
        assert len(mid) == 36


class TestNormalizationAndHash:
    """_normalize_content / _content_hash 单元测试。"""

    def test_normalize_collapse_whitespace(self) -> None:
        assert _normalize_content("  hello   world  ") == "hello world"

    def test_normalize_lowercase(self) -> None:
        assert _normalize_content("Hello WORLD") == "hello world"

    def test_normalize_empty(self) -> None:
        assert _normalize_content("") == ""

    def test_hash_consistent(self) -> None:
        h = _content_hash("测试内容")
        assert h == _content_hash("测试内容")
        assert len(h) == 16

    def test_hash_whitespace_invariant(self) -> None:
        assert _content_hash("hello world") == _content_hash("HELLO  WORLD")

    def test_hash_empty_returns_empty(self) -> None:
        assert _content_hash("") == ""
        assert _content_hash("   ") == ""


class TestRRFFusion:
    """RRF 融合 / tsquery 工具的单元测试。"""

    def test_rrf_fuse_single_list(self) -> None:
        result = _rrf_fuse([["a", "b", "c"]], k=60)
        ids = [doc_id for doc_id, _ in result]
        assert ids == ["a", "b", "c"]

    def test_rrf_fuse_merges_and_dedupes(self) -> None:
        result = _rrf_fuse([["a", "b"], ["a", "c"]], k=60)
        ids = [doc_id for doc_id, _ in result]
        assert ids[0] == "a"
        assert set(ids) == {"a", "b", "c"}

    def test_rrf_fuse_empty(self) -> None:
        assert _rrf_fuse([[], []]) == []

    def test_rrf_fuse_skips_empty_id(self) -> None:
        result = _rrf_fuse([["", "x"]])
        ids = [doc_id for doc_id, _ in result]
        assert ids == ["x"]

    def test_to_tsquery_or_basic(self) -> None:
        assert _to_tsquery_or("hello world") == "hello | world"

    def test_to_tsquery_or_filters_short_and_meta(self) -> None:
        # 单字符 "a" 被过滤;& | 作为非词字符被切分
        assert _to_tsquery_or("a foo&bar | baz") == "foo | bar | baz"

    def test_to_tsquery_or_dedupes(self) -> None:
        assert _to_tsquery_or("foo foo bar") == "foo | bar"

    def test_to_tsquery_or_empty(self) -> None:
        assert _to_tsquery_or("") == ""
        assert _to_tsquery_or("。。。") == ""


# ---------------------------------------------------------------------------
# MemoryRepository 测试
# ---------------------------------------------------------------------------


def _make_memory_repo(**kwargs) -> MemoryRepository:
    engine = sa.create_engine("sqlite://")
    metadata_obj.create_all(engine)
    return MemoryRepository(engine=engine, **kwargs)


class TestMemoryRepository:
    def test_save_returns_uuid(self) -> None:
        repo = _make_memory_repo()
        mid = repo.save("u1", "s1", "insight", "测试内容")
        assert mid is not None and len(mid) == 36

    def test_save_and_search(self) -> None:
        repo = _make_memory_repo()
        repo.save("u1", "s1", "insight", "采购异常分析")
        results = repo.search("u1", "采购")
        assert len(results) == 1

    def test_save_skipped_too_short(self) -> None:
        repo = _make_memory_repo(min_content_len=20)
        assert repo.save("u1", "s1", "t", "短") is None

    def test_save_deduped(self) -> None:
        repo = _make_memory_repo(dedupe_window_seconds=900)
        repo.save("u1", "s1", "t", "同一内容")
        assert repo.save("u1", "s1", "t", "同一内容") is None

    def test_cap_enforced(self) -> None:
        repo = _make_memory_repo(max_per_user=2)
        for i in range(4):
            repo.save("u1", "s1", "t", f"记忆{i}")
        with repo._engine.connect() as conn:
            count = conn.execute(
                sa.select(sa.func.count()).select_from(memories_table)
            ).scalar()
        assert count == 2

    def test_delete_user(self) -> None:
        repo = _make_memory_repo()
        repo.save("u1", "s1", "t", "内容")
        count = repo.delete_user("u1")
        assert count == 1
        with repo._engine.connect() as conn:
            remaining = conn.execute(
                sa.select(sa.func.count()).select_from(memories_table)
            ).scalar()
        assert remaining == 0

    def test_delete_all(self) -> None:
        repo = _make_memory_repo()
        repo.save("u1", "s1", "t", "内容1")
        repo.save("u2", "s2", "t", "内容2")
        count = repo.delete_all()
        assert count == 2

    def test_search_semantic_no_vector(self) -> None:
        repo = _make_memory_repo()
        assert repo.search_semantic("u1", "query") == []

    def test_search_semantic_error_swallowed(self) -> None:
        vs = MagicMock()
        vs.search.side_effect = RuntimeError("boom")
        repo = _make_memory_repo(vector_store=vs)
        assert repo.search_semantic("u1", "query") == []


# ---------------------------------------------------------------------------
# ReportRepository 测试
# ---------------------------------------------------------------------------


def _make_report_repo(**kwargs) -> ReportRepository:
    engine = sa.create_engine("sqlite://")
    metadata_obj.create_all(engine)
    return ReportRepository(engine=engine, **kwargs)


class TestReportRepository:
    def test_save_and_get(self) -> None:
        repo = _make_report_repo()
        rid = repo.save("u1", "s1", "三路匹配", "three_way_match", "{}", "# 报告", 2)
        report = repo.get(rid)
        assert report is not None
        assert report["query"] == "三路匹配"
        assert report["anomaly_count"] == 2

    def test_get_not_found(self) -> None:
        repo = _make_report_repo()
        assert repo.get("nonexistent") is None

    def test_list(self) -> None:
        repo = _make_report_repo()
        repo.save("u1", "s1", "q1", "t", "{}", "", 0)
        repo.save("u1", "s2", "q2", "t", "{}", "", 1)
        repo.save("u2", "s3", "q3", "t", "{}", "", 0)
        reports = repo.list("u1")
        assert len(reports) == 2

    def test_list_limit(self) -> None:
        repo = _make_report_repo()
        for i in range(5):
            repo.save("u1", f"s{i}", f"q{i}", "t", "{}", "", i)
        assert len(repo.list("u1", limit=2)) == 2

    def test_delete_user(self) -> None:
        repo = _make_report_repo()
        repo.save("u1", "s1", "q", "t", "{}", "", 0)
        count = repo.delete_user("u1")
        assert count == 1
        assert repo.list("u1") == []

    def test_delete_all(self) -> None:
        repo = _make_report_repo()
        repo.save("u1", "s1", "q", "t", "{}", "", 0)
        repo.save("u2", "s2", "q", "t", "{}", "", 0)
        count = repo.delete_all()
        assert count == 2

    def test_save_vector_failure_swallowed(self) -> None:
        vs = MagicMock()
        vs.add_documents.side_effect = RuntimeError("vector down")
        repo = _make_report_repo(vector_store=vs)
        rid = repo.save("u1", "s1", "q", "t", "{}", "# 报告", 0)
        assert rid is not None

    def test_search_semantic_no_vector(self) -> None:
        repo = _make_report_repo()
        assert repo.search_semantic("u1", "q") == []

    def test_search_semantic_error_swallowed(self) -> None:
        vs = MagicMock()
        vs.search.side_effect = RuntimeError("boom")
        repo = _make_report_repo(vector_store=vs)
        assert repo.search_semantic("u1", "q") == []

    def test_save_and_get_by_trace_id(self) -> None:
        """save 写入 trace_id 后，get_by_trace_id 能按该列反查到行。
        支撑 /analyze/tasks/{trace_id} 快照接口跨 worker 重建 result。"""
        repo = _make_report_repo()
        tid = "trace-abc"
        rid = repo.save(
            "u1", "s1", "q", "t", '{"x": 1}', "# r", 0,
            trace_id=tid,
        )
        hit = repo.get_by_trace_id(tid)
        assert hit is not None
        assert hit["id"] == rid
        assert hit["trace_id"] == tid
        assert hit["result_json"] == '{"x": 1}'

    def test_get_by_trace_id_not_found(self) -> None:
        repo = _make_report_repo()
        assert repo.get_by_trace_id("nonexistent-trace") is None

    def test_get_by_trace_id_empty_input(self) -> None:
        """空字符串 trace_id 必须直接返回 None，不发起 DB 查询。
        （防御 trace_id 未设置的历史行误伤）"""
        repo = _make_report_repo()
        # 写一行 trace_id=NULL
        repo.save("u1", "s1", "q", "t", "{}", "", 0)
        assert repo.get_by_trace_id("") is None

    def test_trace_id_unique_constraint(self) -> None:
        """同一 trace_id 重复写入应被 UNIQUE 索引拒掉
        （体现"一次 analyze → 一份 report"的业务约束）。"""
        repo = _make_report_repo()
        tid = "trace-dup"
        repo.save("u1", "s1", "q", "t", "{}", "", 0, trace_id=tid)
        with pytest.raises(Exception):  # IntegrityError from SQLite
            repo.save("u1", "s2", "q2", "t", "{}", "", 0, trace_id=tid)


# ---------------------------------------------------------------------------
# _VectorStoreProxy 测试
# ---------------------------------------------------------------------------


class TestVectorStoreProxy:
    def test_get_returns_instance_when_available(self) -> None:
        vs = MagicMock()
        proxy = _VectorStoreProxy(instance=vs)
        assert proxy.get() is vs

    def test_get_returns_none_when_no_factory_no_instance(self) -> None:
        proxy = _VectorStoreProxy()
        assert proxy.get() is None

    def test_factory_called_on_first_get(self) -> None:
        vs = MagicMock()
        factory = MagicMock(return_value=vs)
        proxy = _VectorStoreProxy(factory=factory)
        result = proxy.get()
        assert result is vs
        factory.assert_called_once()

    def test_factory_not_called_again_after_success(self) -> None:
        vs = MagicMock()
        factory = MagicMock(return_value=vs)
        proxy = _VectorStoreProxy(factory=factory)
        proxy.get()
        proxy.get()
        assert factory.call_count == 1

    def test_factory_failure_returns_none(self) -> None:
        factory = MagicMock(side_effect=RuntimeError("down"))
        proxy = _VectorStoreProxy(factory=factory)
        assert proxy.get() is None

    def test_factory_not_retried_during_backoff(self) -> None:
        factory = MagicMock(side_effect=RuntimeError("down"))
        proxy = _VectorStoreProxy(factory=factory)
        proxy.get()  # 失败，进入退避
        proxy.get()  # 退避中，不调 factory
        assert factory.call_count == 1

    def test_factory_retried_after_backoff_expires(self) -> None:
        from datetime import datetime, timezone
        vs = MagicMock()
        call_count = 0

        def factory():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("down")
            return vs

        proxy = _VectorStoreProxy(factory=factory)
        proxy.get()  # 失败，进入退避
        # 强制退避过期
        proxy._next_retry_at = datetime.min.replace(tzinfo=timezone.utc)
        result = proxy.get()  # 应重试并成功
        assert result is vs
        assert call_count == 2

    def test_backoff_doubles_on_repeated_failure(self) -> None:
        factory = MagicMock(side_effect=RuntimeError("down"))
        proxy = _VectorStoreProxy(factory=factory)
        initial_backoff = proxy._backoff

        proxy.get()  # 第一次失败
        backoff_1 = proxy._backoff

        proxy._next_retry_at = proxy._next_retry_at.replace(year=2000)  # 强制过期
        proxy.get()  # 第二次失败
        backoff_2 = proxy._backoff

        assert backoff_1 == initial_backoff * 2
        assert backoff_2 == initial_backoff * 4

    def test_invalidate_allows_immediate_retry(self) -> None:
        vs = MagicMock()
        factory = MagicMock(return_value=vs)
        proxy = _VectorStoreProxy(factory=factory)
        proxy.get()  # 成功，vs 已缓存
        proxy.invalidate()  # 失效
        proxy.get()  # 重新调 factory
        assert factory.call_count == 2

    def test_proxy_used_by_memory_repo_save(self) -> None:
        """MemoryRepository 在 vector_store 不可用时仍能正常写入 SQL。"""
        engine = sa.create_engine("sqlite://")
        metadata_obj.create_all(engine)
        failing_factory = MagicMock(side_effect=RuntimeError("down"))
        proxy = _VectorStoreProxy(factory=failing_factory)
        repo = MemoryRepository.__new__(MemoryRepository)
        repo._engine = engine
        repo._vector_store = proxy
        repo._fusion_k = 60
        repo._max_per_user = 0
        repo._min_content_len = 0
        repo._dedupe_window_seconds = 0
        repo._skip_empty_conclusions = False
        mid = repo.save("u1", "s1", "t", "内容")
        assert mid is not None


