"""DAGCaseStore 单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from config.settings import Settings
from core.orchestrator.dag.case_store import DAGCaseStore


@pytest.fixture()
def settings() -> Settings:
    return Settings()


@pytest.fixture()
def store(settings: Settings) -> DAGCaseStore:
    return DAGCaseStore(settings=settings)


_SAMPLE_DAG = [
    {"task_id": "t1", "tool_name": "query_purchase_orders", "output_key": "orders"},
]


class TestStoreSuccessfulCase:
    @pytest.mark.asyncio
    async def test_skip_non_completed(self, store: DAGCaseStore) -> None:
        """非 completed 状态不写入。"""
        await store.store_successful_case(
            query="测试", analysis_type="three_way_match",
            dag=_SAMPLE_DAG, route_type="DAG",
            exec_result={"status": "failed"},
        )
        # 不应调用 _write_to_pg

    @pytest.mark.asyncio
    async def test_skip_with_failed_tasks(self, store: DAGCaseStore) -> None:
        """有失败任务不写入。"""
        await store.store_successful_case(
            query="测试", analysis_type="three_way_match",
            dag=_SAMPLE_DAG, route_type="DAG",
            exec_result={"status": "completed", "failed_tasks": ["t1"]},
        )

    @pytest.mark.asyncio
    async def test_writes_to_pg_and_chroma(self, store: DAGCaseStore) -> None:
        """成功案例应同时写入 PG 和 Chroma。"""
        store._write_to_pg = MagicMock(return_value=True)
        store._write_to_chroma = MagicMock(return_value=True)

        await store.store_successful_case(
            query="分析三路匹配", analysis_type="three_way_match",
            dag=_SAMPLE_DAG, route_type="DAG",
            exec_result={"status": "completed", "failed_tasks": [], "duration_sec": 1.5},
        )

        store._write_to_pg.assert_called_once()
        store._write_to_chroma.assert_called_once()


class TestWriteToPg:
    def test_session_init_failure(self, store: DAGCaseStore) -> None:
        """session_factory 初始化失败返回 False。"""
        with patch.object(store, "_ensure_session_factory", side_effect=RuntimeError("DB down")):
            result = store._write_to_pg(
                query_hash="abc", query="测试",
                analysis_type="three_way_match", dag=_SAMPLE_DAG,
                route_type="DAG", duration_sec=1.0,
            )
        assert result is False

    def test_write_failure(self, store: DAGCaseStore) -> None:
        """DB 写入失败返回 False。"""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.execute.side_effect = RuntimeError("write error")
        mock_factory = MagicMock(return_value=mock_session)
        store._session_factory = mock_factory

        result = store._write_to_pg(
            query_hash="abc", query="测试",
            analysis_type="three_way_match", dag=_SAMPLE_DAG,
            route_type="DAG", duration_sec=1.0,
        )
        assert result is False


class TestWriteToChroma:
    def test_chroma_init_failure(self, store: DAGCaseStore) -> None:
        """Chroma 初始化失败返回 False。"""
        with patch.object(store, "_ensure_chroma", side_effect=RuntimeError("Chroma down")):
            result = store._write_to_chroma(
                query_hash="abc", query="测试",
                analysis_type="three_way_match", dag=_SAMPLE_DAG,
                route_type="DAG", duration_sec=1.0,
            )
        assert result is False

    def test_chroma_write_success(self, store: DAGCaseStore) -> None:
        """Chroma 写入成功返回 True。"""
        mock_store = MagicMock()
        store._chroma_store = mock_store

        result = store._write_to_chroma(
            query_hash="abc", query="测试",
            analysis_type="three_way_match", dag=_SAMPLE_DAG,
            route_type="DAG", duration_sec=1.0,
        )
        assert result is True
        mock_store.add_documents.assert_called_once()

    def test_chroma_write_failure(self, store: DAGCaseStore) -> None:
        """Chroma 写入异常返回 False。"""
        mock_store = MagicMock()
        mock_store.add_documents.side_effect = RuntimeError("chroma error")
        store._chroma_store = mock_store

        result = store._write_to_chroma(
            query_hash="abc", query="测试",
            analysis_type="three_way_match", dag=_SAMPLE_DAG,
            route_type="DAG", duration_sec=1.0,
        )
        assert result is False


class TestLoadCasesFromPg:
    def test_pg_init_failure(self, store: DAGCaseStore) -> None:
        """PG 初始化失败返回 0。"""
        with patch.object(store, "_ensure_session_factory", side_effect=RuntimeError("DB")):
            assert store.load_cases_from_pg() == 0

    def test_pg_read_failure(self, store: DAGCaseStore) -> None:
        """PG 读取失败返回 0。"""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.execute.side_effect = RuntimeError("read error")
        mock_factory = MagicMock(return_value=mock_session)
        store._session_factory = mock_factory

        assert store.load_cases_from_pg() == 0

    def test_empty_rows(self, store: DAGCaseStore) -> None:
        """PG 无数据返回 0。"""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_session.execute.return_value = mock_result
        mock_factory = MagicMock(return_value=mock_session)
        store._session_factory = mock_factory

        assert store.load_cases_from_pg() == 0

    def test_chroma_init_failure_on_load(self, store: DAGCaseStore) -> None:
        """有 PG 数据但 Chroma 初始化失败返回 0。"""
        mock_row = MagicMock()
        mock_row.query_hash = "abc"
        mock_row.query = "测试"
        mock_row.analysis_type = "three_way_match"
        mock_row.task_count = 1
        mock_row.duration_sec = 1.0
        mock_row.route_type = "DAG"
        mock_row.dag_definition = [{"task_id": "t1"}]

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [mock_row]
        mock_session.execute.return_value = mock_result
        mock_factory = MagicMock(return_value=mock_session)
        store._session_factory = mock_factory

        with patch.object(store, "_ensure_chroma", side_effect=RuntimeError("Chroma init")):
            assert store.load_cases_from_pg() == 0

    def test_load_success(self, store: DAGCaseStore) -> None:
        """成功加载案例到 Chroma。"""
        mock_row = MagicMock()
        mock_row.query_hash = "abc"
        mock_row.query = "测试"
        mock_row.analysis_type = "three_way_match"
        mock_row.task_count = 1
        mock_row.duration_sec = 1.0
        mock_row.route_type = "DAG"
        mock_row.dag_definition = [{"task_id": "t1"}]

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [mock_row]
        mock_session.execute.return_value = mock_result
        mock_factory = MagicMock(return_value=mock_session)
        store._session_factory = mock_factory

        mock_chroma = MagicMock()
        store._chroma_store = mock_chroma

        assert store.load_cases_from_pg() == 1
        mock_chroma.add_documents.assert_called_once()

    def test_chroma_batch_load_failure(self, store: DAGCaseStore) -> None:
        """Chroma 批量写入失败返回 0。"""
        mock_row = MagicMock()
        mock_row.query_hash = "abc"
        mock_row.query = "测试"
        mock_row.analysis_type = "three_way_match"
        mock_row.task_count = 1
        mock_row.duration_sec = 1.0
        mock_row.route_type = "DAG"
        mock_row.dag_definition = '{"task_id": "t1"}'

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [mock_row]
        mock_session.execute.return_value = mock_result
        mock_factory = MagicMock(return_value=mock_session)
        store._session_factory = mock_factory

        mock_chroma = MagicMock()
        mock_chroma.add_documents.side_effect = RuntimeError("batch fail")
        store._chroma_store = mock_chroma

        assert store.load_cases_from_pg() == 0
