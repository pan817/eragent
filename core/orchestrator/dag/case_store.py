"""
DAG 案例存储器（自学习闭环）。

PostgreSQL 为权威数据源，Chroma 为检索缓存。
写入路径：成功 DAG → PG（事务保证）→ Chroma（可重试）。
启动路径：PG 全量读取 → 批量 upsert 到 Chroma。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.logging_utils import get_logger

_logger = get_logger(__name__)

_CASE_COLLECTION = "intent_cases"


class DAGCaseStore:
    """成功 DAG 案例存储器（PG 权威 + Chroma 缓存）。"""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._chroma_store: Any = None
        self._session_factory: Any = None

    # ── 延迟初始化 ─────────────────────────────────────────────────

    def _ensure_chroma(self) -> Any:
        """延迟初始化 Chroma intent_cases collection。"""
        if self._chroma_store is not None:
            return self._chroma_store
        from core.knowledge.vector_store import VectorStore

        store = VectorStore.from_settings(self._settings, _CASE_COLLECTION)
        store.initialize()
        self._chroma_store = store
        return store

    def _ensure_session_factory(self) -> Any:
        """延迟获取 SQLAlchemy session factory。"""
        if self._session_factory is not None:
            return self._session_factory
        from core.database.engine import get_session_factory, get_engine

        engine = get_engine()
        self._session_factory = get_session_factory(engine)
        return self._session_factory

    # ── 写入（PG 先写 + Chroma 后写） ──────────────────────────────

    async def store_successful_case(
        self,
        query: str,
        analysis_type: str,
        dag: list[dict[str, Any]],
        route_type: str,
        exec_result: dict[str, Any],
    ) -> None:
        """将成功执行的 DAG 案例持久化。

        仅存储真正成功的案例（status == completed 且无失败任务）。
        写入顺序：PG（权威）→ Chroma（缓存），任一失败均记日志不阻断。

        Args:
            query: 用户原始查询。
            analysis_type: 分析类型字符串。
            dag: DAG 任务定义列表。
            route_type: 路由类型（DAG / ReAct）。
            exec_result: 执行结果字典。
        """
        from core.observability.middleware import record_span

        if exec_result.get("status") != "ok":
            return
        if exec_result.get("failed_tasks"):
            return

        query_hash = hashlib.md5(query.encode()).hexdigest()[:16]
        duration_sec = exec_result.get("duration_sec", 0)

        with record_span("case_store", "store_dag_case") as span_attrs:
            span_attrs["query_hash"] = query_hash
            span_attrs["analysis_type"] = analysis_type
            span_attrs["task_count"] = len(dag)

            # 1. 写入 PostgreSQL（权威数据源）
            pg_ok = self._write_to_pg(
                query_hash=query_hash,
                query=query,
                analysis_type=analysis_type,
                dag=dag,
                route_type=route_type,
                duration_sec=duration_sec,
                span_attrs=span_attrs,
            )
            span_attrs["pg_ok"] = pg_ok

            # 2. 写入 Chroma（检索缓存）
            chroma_ok = self._write_to_chroma(
                query_hash=query_hash,
                query=query,
                analysis_type=analysis_type,
                dag=dag,
                route_type=route_type,
                duration_sec=duration_sec,
                span_attrs=span_attrs,
            )
            span_attrs["chroma_ok"] = chroma_ok

    def _write_to_pg(
        self,
        query_hash: str,
        query: str,
        analysis_type: str,
        dag: list[dict[str, Any]],
        route_type: str,
        duration_sec: float,
        span_attrs: dict[str, Any] | None = None,
    ) -> bool:
        """写入 PostgreSQL（upsert by query_hash）。返回是否成功。"""
        try:
            session_factory = self._ensure_session_factory()
        except Exception as exc:
            _logger.warning("dag case PG session init failed: %s", exc)
            if span_attrs is not None:
                span_attrs["pg_error"] = f"{type(exc).__name__}: {exc}"
            return False

        from core.orchestrator.dag.tables import dag_cases_table
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        try:
            with session_factory() as session:
                stmt = pg_insert(dag_cases_table).values(
                    query_hash=query_hash,
                    query=query,
                    analysis_type=analysis_type,
                    dag_definition=dag,
                    route_type=route_type,
                    task_count=len(dag),
                    duration_sec=duration_sec,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["query_hash"],
                    set_={
                        "dag_definition": dag,
                        "route_type": route_type,
                        "task_count": len(dag),
                        "duration_sec": duration_sec,
                        "updated_at": stmt.excluded.created_at,
                    },
                )
                session.execute(stmt)
                session.commit()
                _logger.info("DAG case saved to PG: hash=%s type=%s", query_hash, analysis_type)
                return True
        except Exception as exc:
            _logger.warning("DAG case PG write failed: %s", exc)
            if span_attrs is not None:
                span_attrs["pg_error"] = f"{type(exc).__name__}: {exc}"
            return False

    def _write_to_chroma(
        self,
        query_hash: str,
        query: str,
        analysis_type: str,
        dag: list[dict[str, Any]],
        route_type: str,
        duration_sec: float,
        span_attrs: dict[str, Any] | None = None,
    ) -> bool:
        """写入 Chroma（检索缓存）。返回是否成功。"""
        try:
            store = self._ensure_chroma()
        except Exception as exc:
            _logger.warning("dag case Chroma init failed: %s", exc)
            if span_attrs is not None:
                span_attrs["chroma_error"] = f"{type(exc).__name__}: {exc}"
            return False

        doc_id = f"dag_case_{query_hash}"
        case_text = (
            f"成功案例：{query}\n"
            f"分析类型：{analysis_type}\n"
            f"任务数：{len(dag)}\n"
            f"执行时长：{duration_sec}秒"
        )

        doc = {
            "id": doc_id,
            "text": case_text,
            "metadata": {
                "analysis_type": analysis_type,
                "route_type": route_type,
                "task_count": len(dag),
                "dag_definition": json.dumps(dag, ensure_ascii=False),
            },
        }

        try:
            store.add_documents([doc])
            _logger.info("DAG case cached to Chroma: %s", doc_id)
            return True
        except Exception as exc:
            _logger.warning("DAG case Chroma write failed: %s", exc)
            if span_attrs is not None:
                span_attrs["chroma_error"] = f"{type(exc).__name__}: {exc}"
            return False

    # ── 启动加载（PG → Chroma） ────────────────────────────────────

    def load_cases_from_pg(self) -> int:
        """从 PostgreSQL 全量加载案例到 Chroma。

        服务启动时调用。Chroma 初始化失败不阻塞启动。

        Returns:
            加载的案例数量。
        """
        try:
            session_factory = self._ensure_session_factory()
        except Exception as exc:
            _logger.warning("dag case PG init failed on startup, skip loading: %s", exc)
            return 0

        from core.orchestrator.dag.tables import dag_cases_table
        from sqlalchemy import select

        try:
            with session_factory() as session:
                rows = session.execute(
                    select(dag_cases_table).order_by(dag_cases_table.c.updated_at.desc())
                ).fetchall()
        except Exception as exc:
            _logger.warning("dag case PG read failed: %s", exc)
            return 0

        if not rows:
            _logger.info("no DAG cases in PG, skip Chroma loading")
            return 0

        try:
            store = self._ensure_chroma()
        except Exception as exc:
            _logger.warning("Chroma init failed on startup, cases not loaded: %s", exc)
            return 0

        docs: list[dict[str, Any]] = []
        for row in rows:
            doc_id = f"dag_case_{row.query_hash}"
            dag_def = row.dag_definition
            case_text = (
                f"成功案例：{row.query}\n"
                f"分析类型：{row.analysis_type}\n"
                f"任务数：{row.task_count}\n"
                f"执行时长：{row.duration_sec or 0}秒"
            )
            docs.append({
                "id": doc_id,
                "text": case_text,
                "metadata": {
                    "analysis_type": row.analysis_type,
                    "route_type": row.route_type,
                    "task_count": row.task_count,
                    "dag_definition": json.dumps(dag_def, ensure_ascii=False)
                    if not isinstance(dag_def, str)
                    else dag_def,
                },
            })

        try:
            store.add_documents(docs)
            _logger.info("loaded %d DAG cases from PG to Chroma", len(docs))
            return len(docs)
        except Exception as exc:
            _logger.warning("Chroma batch load failed: %s", exc)
            return 0
