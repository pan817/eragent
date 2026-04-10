"""长期记忆：基于 PostgreSQL 的记忆和报告持久化。"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import sqlalchemy as sa

from core.memory.tables import memories_table, metadata_obj, reports_table

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模块级计数器（轻量 metric，无需第三方库）
# ---------------------------------------------------------------------------

_counters_lock = threading.Lock()
_counters: dict[str, int] = defaultdict(int)


def _inc(key: str) -> None:
    """线程安全地将计数器 key 加一。"""
    with _counters_lock:
        _counters[key] += 1


def get_counters() -> dict[str, int]:
    """返回当前所有计数器的快照，供测试和管理端查询。"""
    with _counters_lock:
        return dict(_counters)


# ---------------------------------------------------------------------------
# VectorStore 懒重试代理：解决单例永久降级问题
# ---------------------------------------------------------------------------

_PROXY_INITIAL_BACKOFF = 60       # 首次失败后退避 60s
_PROXY_MAX_BACKOFF = 3600         # 退避上限 1h


class _VectorStoreProxy:
    """对 VectorStore 实例的懒重试代理。

    解决单例永久降级问题：VectorStore 初始化失败后不再永久持有 None，
    而是按指数退避策略定期重试，恢复后自动重新挂载。

    线程安全：所有状态变更在 ``_lock`` 保护下执行。

    Usage::

        # 传入 factory（用于单例场景，factory 负责初始化）
        proxy = _VectorStoreProxy(factory=build_vs)

        # 传入已有实例（兼容测试注入）
        proxy = _VectorStoreProxy(instance=vs)
    """

    def __init__(
        self,
        factory: Any = None,
        instance: Any = None,
    ) -> None:
        self._factory = factory
        self._vs: Any = instance
        self._next_retry_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._backoff: float = _PROXY_INITIAL_BACKOFF
        self._lock = threading.Lock()

    def get(self) -> Any:
        """返回可用的 VectorStore 实例，不可用时返回 None。

        - 实例已就绪：直接返回。
        - 无 factory 且实例为 None：返回 None（纯 SQL 模式）。
        - 退避窗口内：返回 None，不重试。
        - 退避过期：尝试调 factory；成功则重置退避并返回实例；
          失败则按指数策略延长退避时间，返回 None。
        """
        with self._lock:
            if self._vs is not None:
                return self._vs
            if self._factory is None:
                return None
            now = datetime.now(timezone.utc)
            if now < self._next_retry_at:
                return None
            try:
                self._vs = self._factory()
                self._backoff = _PROXY_INITIAL_BACKOFF
                _logger.info("ltm.vector_store.recovered")
                return self._vs
            except Exception as exc:  # noqa: BLE001
                self._next_retry_at = now + timedelta(seconds=self._backoff)
                self._backoff = min(self._backoff * 2, _PROXY_MAX_BACKOFF)
                _logger.warning(
                    "ltm.vector_store.init_failed retry_in=%.0fs error=%s",
                    self._backoff / 2,
                    exc,
                )
                return None

    def invalidate(self) -> None:
        """强制将当前实例标记为失效，下次 get() 时立即重试。"""
        with self._lock:
            self._vs = None
            self._next_retry_at = datetime.min.replace(tzinfo=timezone.utc)
            self._backoff = _PROXY_INITIAL_BACKOFF


# ---------------------------------------------------------------------------
# 写入策略辅助：归一化 + 内容指纹
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_content(text: str) -> str:
    """归一化文本：折叠空白 + 去首尾 + 转小写。

    用于内容指纹计算，使得仅空白/大小写差异的内容被视为同一条。
    """
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def _content_hash(text: str) -> str:
    """计算 content 的 16 字符 sha256 指纹。"""
    normalized = _normalize_content(text)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Hybrid 检索辅助
# ---------------------------------------------------------------------------

# 切分非词非 CJK 字符（空白、ASCII/全角标点等）
_TOKEN_SPLIT_RE = re.compile(r"[^\w\u4e00-\u9fff]+", flags=re.UNICODE)

# tsquery 元字符，需要从 token 中剔除
_TSQUERY_META_CHARS = set("&|!():*'\"")


def _to_tsquery_or(text: str) -> str:
    """把自然语言 query 转成 PostgreSQL tsquery 的 OR 形式。

    - 按非词字符切分
    - 过滤长度 < 2 的 token（单字符噪声）
    - 剔除 tsquery 元字符
    - 去重保序
    返回空串表示无可用 token，调用方应跳过 FTS 路径。
    """
    if not text:
        return ""
    parts = _TOKEN_SPLIT_RE.split(text.strip())
    seen: set[str] = set()
    cleaned: list[str] = []
    for part in parts:
        safe = "".join(c for c in part if c not in _TSQUERY_META_CHARS).strip()
        if len(safe) < 2 or safe in seen:
            continue
        seen.add(safe)
        cleaned.append(safe)
    return " | ".join(cleaned)


def _rrf_fuse(
    rank_lists: Iterable[list[str]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: 把多路 rank 列表融合为一个有序结果。

    Args:
        rank_lists: 每一路的 doc_id 列表，已按各自的相关度从高到低排好。
        k: RRF 平滑常数，业界默认 60。

    Returns:
        ``[(doc_id, fused_score), ...]`` 按融合分数从高到低排序。
    """
    scores: dict[str, float] = {}
    for ranks in rank_lists:
        for rank, doc_id in enumerate(ranks):
            if not doc_id:
                continue
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])


class LongTermMemory:
    """Facade：组合 MemoryRepository 与 ReportRepository，保持向后兼容的公开 API。

    新代码应优先直接使用 ``MemoryRepository`` / ``ReportRepository``；
    此类仅为现有调用方（agent、API 路由）提供过渡兼容层。
    """

    def __init__(
        self,
        dsn: str = "",
        vector_store: Any = None,
        engine: sa.engine.Engine | None = None,
        fusion_k: int = 60,
        max_per_user: int = 0,
        min_content_len: int = 0,
        dedupe_window_seconds: int = 0,
        skip_empty_conclusions: bool = False,
    ) -> None:
        if engine is None:
            from core.database.engine import create_engine_from_dsn
            engine = create_engine_from_dsn(dsn, pool_size=5, max_overflow=10, pool_pre_ping=True)

        self._mem_repo = MemoryRepository(
            engine=engine,
            vector_store=vector_store,
            fusion_k=fusion_k,
            max_per_user=max_per_user,
            min_content_len=min_content_len,
            dedupe_window_seconds=dedupe_window_seconds,
            skip_empty_conclusions=skip_empty_conclusions,
        )
        self._rep_repo = ReportRepository(engine=engine, vector_store=vector_store)

    def init_tables(self) -> None:
        self._mem_repo.init_tables()

    # ------------------------------------------------------------------
    # memories — 委托给 MemoryRepository
    # ------------------------------------------------------------------

    def save_memory(
        self,
        user_id: str,
        session_id: str,
        memory_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        return self._mem_repo.save(user_id, session_id, memory_type, content, metadata)

    def search_memories(self, user_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._mem_repo.search(user_id, query, limit)

    def search_memories_semantic(self, user_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._mem_repo.search_semantic(user_id, query, limit)

    # ------------------------------------------------------------------
    # reports — 委托给 ReportRepository
    # ------------------------------------------------------------------

    def save_report(
        self,
        user_id: str,
        session_id: str,
        query: str,
        analysis_type: str,
        result_json: str,
        report_markdown: str,
        anomaly_count: int,
        report_id: str | None = None,
    ) -> str:
        return self._rep_repo.save(
            user_id, session_id, query, analysis_type,
            result_json, report_markdown, anomaly_count, report_id,
        )

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        return self._rep_repo.get(report_id)

    def search_reports_semantic(self, user_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._rep_repo.search_semantic(user_id, query, limit)

    def list_reports(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._rep_repo.list(user_id, limit)

    # ------------------------------------------------------------------
    # 删除 / 重置 — 委托给两个 repo
    # ------------------------------------------------------------------

    def delete_user_data(
        self,
        user_id: str,
        *,
        delete_memories: bool = True,
        delete_reports: bool = True,
    ) -> dict[str, int]:
        deleted = {"memories": 0, "reports": 0}
        if delete_memories:
            deleted["memories"] = self._mem_repo.delete_user(user_id)
        if delete_reports:
            deleted["reports"] = self._rep_repo.delete_user(user_id)
        return deleted

    def delete_all(self) -> dict[str, int]:
        """清空 memories 与 reports 全部记录。仅供管理端使用。"""
        return {
            "memories": self._mem_repo.delete_all(),
            "reports": self._rep_repo.delete_all(),
        }


# ---------------------------------------------------------------------------
# 模块级懒单例
# ---------------------------------------------------------------------------

_long_term_memory_singleton: LongTermMemory | None = None


def get_long_term_memory() -> LongTermMemory:
    """获取进程级 LongTermMemory facade 单例（委托给 get_memory_repository / get_report_repository）。"""
    global _long_term_memory_singleton
    if _long_term_memory_singleton is None:
        mem_repo = get_memory_repository()
        rep_repo = get_report_repository()
        ltm = LongTermMemory.__new__(LongTermMemory)
        ltm._mem_repo = mem_repo
        ltm._rep_repo = rep_repo
        _long_term_memory_singleton = ltm
    return _long_term_memory_singleton


def reset_long_term_memory() -> None:
    """重置单例。仅供测试使用。"""
    global _long_term_memory_singleton
    _long_term_memory_singleton = None


# ---------------------------------------------------------------------------
# MemoryRepository：专管 memories 表的读写、过滤、淘汰
# ---------------------------------------------------------------------------


class MemoryRepository:
    """管理 memories 表的读写、过滤、cap 淘汰与 hybrid 召回。

    与 ``LongTermMemory`` 相比，只负责记忆片段，不涉及报告。
    生命周期策略（TTL / cap / dedup）全部集中在此类。
    """

    def __init__(
        self,
        engine: sa.engine.Engine,
        vector_store: Any = None,
        fusion_k: int = 60,
        max_per_user: int = 0,
        min_content_len: int = 0,
        dedupe_window_seconds: int = 0,
        skip_empty_conclusions: bool = False,
    ) -> None:
        self._engine = engine
        self._vector_store = _VectorStoreProxy(instance=vector_store)
        self._fusion_k = fusion_k
        self._max_per_user = max_per_user
        self._min_content_len = min_content_len
        self._dedupe_window_seconds = dedupe_window_seconds
        self._skip_empty_conclusions = skip_empty_conclusions

    def init_tables(self) -> None:
        """创建 memories 表及相关索引（幂等）。"""
        metadata_obj.create_all(self._engine, checkfirst=True)
        if self._engine.dialect.name == "postgresql":
            with self._engine.connect() as conn:
                conn.execute(
                    sa.text(
                        "CREATE INDEX IF NOT EXISTS memories_content_fts "
                        "ON memories USING GIN (to_tsvector('simple', content))"
                    )
                )
                conn.execute(
                    sa.text(
                        "CREATE INDEX IF NOT EXISTS memories_user_created "
                        "ON memories (user_id, created_at DESC)"
                    )
                )
                conn.commit()

    def save(
        self,
        user_id: str,
        session_id: str,
        memory_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """写入一条记忆，经三层过滤后决定是否实际写入。

        Returns:
            写入成功时返回 memory_id；被过滤跳过时返回 None。
        """
        if metadata is None:
            metadata = {}

        # L1: 内容长度
        if self._min_content_len > 0 and len(content.strip()) < self._min_content_len:
            _inc("ltm.save.skipped.too_short")
            _logger.info(
                "ltm.save.skipped reason=too_short user_id=%s memory_type=%s "
                "content_len=%d threshold=%d",
                user_id, memory_type, len(content.strip()), self._min_content_len,
            )
            return None

        # L2: 内容指纹去重
        chash = _content_hash(content)
        if self._dedupe_window_seconds > 0 and chash:
            window_start = datetime.now(timezone.utc) - timedelta(
                seconds=self._dedupe_window_seconds
            )
            dup_stmt = (
                sa.select(sa.func.count())
                .select_from(memories_table)
                .where(
                    sa.and_(
                        memories_table.c.user_id == user_id,
                        memories_table.c.content_hash == chash,
                        memories_table.c.created_at >= window_start,
                    )
                )
            )
            try:
                with self._engine.connect() as conn:
                    dup_count: int = conn.execute(dup_stmt).scalar() or 0
            except Exception as exc:  # noqa: BLE001
                _inc("ltm.dedupe.error")
                _logger.warning("ltm.dedupe_check.error user_id=%s error=%s", user_id, exc)
                dup_count = 0
            if dup_count > 0:
                _inc("ltm.save.skipped.duplicate")
                _logger.info(
                    "ltm.save.skipped reason=duplicate user_id=%s memory_type=%s "
                    "content_hash=%s window_seconds=%d",
                    user_id, memory_type, chash, self._dedupe_window_seconds,
                )
                return None

        # L3: 空结论过滤
        if self._skip_empty_conclusions:
            if metadata.get("anomaly_count") == 0 and not metadata.get("summary", ""):
                _inc("ltm.save.skipped.empty_conclusion")
                _logger.info(
                    "ltm.save.skipped reason=empty_conclusion user_id=%s memory_type=%s",
                    user_id, memory_type,
                )
                return None

        memory_id = str(uuid.uuid4())
        stmt = memories_table.insert().values(
            id=memory_id,
            user_id=user_id,
            session_id=session_id,
            memory_type=memory_type,
            content=content,
            content_hash=chash or None,
            attrs=metadata,
            created_at=datetime.now(timezone.utc),
        )
        try:
            with self._engine.connect() as conn:
                conn.execute(stmt)
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            _inc("ltm.save.error")
            _logger.error(
                "ltm.save.error user_id=%s memory_type=%s error=%s",
                user_id, memory_type, exc,
            )
            raise

        _inc("ltm.save.written")
        _logger.info(
            "ltm.save.written user_id=%s memory_type=%s memory_id=%s",
            user_id, memory_type, memory_id,
        )

        # 向量旁路（best-effort）
        _vs = self._vector_store.get()
        if _vs is not None:
            try:
                _vs.add_documents([{
                    "id": f"memory_{memory_id}",
                    "text": content,
                    "metadata": {
                        "source": "memory",
                        "memory_id": memory_id,
                        "user_id": user_id,
                        "session_id": session_id,
                        "memory_type": memory_type,
                    },
                }])
            except Exception as exc:  # noqa: BLE001
                _inc("ltm.vector.write.error")
                _logger.warning(
                    "ltm.vector.write.error memory_id=%s user_id=%s error=%s",
                    memory_id, user_id, exc,
                )

        # 滚动 cap
        if self._max_per_user > 0:
            try:
                self._enforce_user_cap(user_id)
            except Exception as exc:  # noqa: BLE001
                _inc("ltm.enforce_cap.error")
                _logger.warning("ltm.enforce_cap.failed user_id=%s error=%s", user_id, exc)

        return memory_id

    def _enforce_user_cap(self, user_id: str) -> None:
        cap = self._max_per_user
        with self._engine.connect() as conn:
            select_stmt = (
                sa.select(memories_table.c.id)
                .where(memories_table.c.user_id == user_id)
                .order_by(memories_table.c.created_at.desc())
                .offset(cap)
            )
            stale_ids = [row[0] for row in conn.execute(select_stmt)]
            if not stale_ids:
                return
            conn.execute(memories_table.delete().where(memories_table.c.id.in_(stale_ids)))
            conn.commit()

        _vs = self._vector_store.get()
        if _vs is not None:
            try:
                _vs.delete(ids=[f"memory_{i}" for i in stale_ids])
            except Exception as exc:  # noqa: BLE001
                _inc("ltm.vector.delete.error")
                _logger.warning(
                    "ltm.vector.delete.error user_id=%s n_ids=%d error=%s",
                    user_id, len(stale_ids), exc,
                )

    def search(self, user_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Hybrid 召回（稀疏 + 稠密 + RRF 融合）。"""
        candidate_size = max(limit * 4, limit)
        sparse_ids = self._sparse_search_ids(user_id, query, candidate_size)
        dense_ids = self._dense_search_ids(user_id, query, candidate_size)
        if not sparse_ids and not dense_ids:
            return []
        fused = _rrf_fuse([sparse_ids, dense_ids], k=self._fusion_k)
        top_ids = [doc_id for doc_id, _ in fused[:limit]]
        if not top_ids:
            return []
        rows_by_id = self._fetch_by_ids(user_id, top_ids)
        return [rows_by_id[mid] for mid in top_ids if mid in rows_by_id]

    def search_semantic(self, user_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """纯语义召回（需要 vector_store）。"""
        _vs = self._vector_store.get()
        if _vs is None:
            return []
        try:
            hits = _vs.search(
                query=query,
                top_k=limit,
                where={"user_id": user_id, "source": "memory"},
            )
        except Exception as exc:  # noqa: BLE001
            _inc("ltm.vector.search.error")
            _logger.warning("ltm.vector.search.error user_id=%s error=%s", user_id, exc)
            return []
        return hits

    def _sparse_search_ids(self, user_id: str, query: str, limit: int) -> list[str]:
        if not query or not query.strip():
            return []
        if self._engine.dialect.name == "postgresql":
            ts_query = _to_tsquery_or(query)
            if not ts_query:
                return []
            ts_vec = sa.func.to_tsvector("simple", memories_table.c.content)
            ts_q = sa.func.to_tsquery("simple", ts_query)
            stmt = (
                sa.select(memories_table.c.id)
                .where(sa.and_(memories_table.c.user_id == user_id, ts_vec.op("@@")(ts_q)))
                .order_by(sa.func.ts_rank(ts_vec, ts_q).desc(), memories_table.c.created_at.desc())
                .limit(limit)
            )
        else:
            stmt = (
                sa.select(memories_table.c.id)
                .where(sa.and_(
                    memories_table.c.user_id == user_id,
                    memories_table.c.content.like(f"%{query}%"),
                ))
                .order_by(memories_table.c.created_at.desc())
                .limit(limit)
            )
        try:
            with self._engine.connect() as conn:
                return [row[0] for row in conn.execute(stmt)]
        except Exception as exc:  # noqa: BLE001
            _inc("ltm.search.error")
            _logger.warning("ltm.sparse_search.error user_id=%s error=%s", user_id, exc)
            return []

    def _dense_search_ids(self, user_id: str, query: str, limit: int) -> list[str]:
        _vs = self._vector_store.get()
        if _vs is None or not query:
            return []
        try:
            hits = _vs.search(
                query=query,
                top_k=limit,
                where={"user_id": user_id, "source": "memory"},
            )
        except Exception as exc:  # noqa: BLE001
            _inc("ltm.vector.search.error")
            _logger.warning("ltm.dense_search.error user_id=%s error=%s", user_id, exc)
            return []
        ids: list[str] = []
        for hit in hits or []:
            doc_id = str(hit.get("id", ""))
            if doc_id.startswith("memory_"):
                ids.append(doc_id[len("memory_"):])
            else:
                mid = (hit.get("metadata") or {}).get("memory_id")
                if mid:
                    ids.append(str(mid))
        return ids

    def _fetch_by_ids(self, user_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        stmt = sa.select(memories_table).where(
            sa.and_(memories_table.c.user_id == user_id, memories_table.c.id.in_(ids))
        )
        with self._engine.connect() as conn:
            return {dict(row._mapping)["id"]: dict(row._mapping) for row in conn.execute(stmt)}

    def delete_user(self, user_id: str) -> int:
        """删除指定用户的全部记忆，返回删除行数。"""
        with self._engine.connect() as conn:
            res = conn.execute(memories_table.delete().where(memories_table.c.user_id == user_id))
            count = res.rowcount or 0
            conn.commit()
        _vs = self._vector_store.get()
        if _vs is not None:
            try:
                _vs.delete(where={"user_id": user_id})
            except Exception as exc:  # noqa: BLE001
                _inc("ltm.vector.delete.error")
                _logger.warning(
                    "ltm.vector.delete.error (user_data) user_id=%s error=%s", user_id, exc
                )
        return count

    def delete_all(self) -> int:
        """清空 memories 表全部记录，返回删除行数。"""
        with self._engine.connect() as conn:
            res = conn.execute(memories_table.delete())
            count = res.rowcount or 0
            conn.commit()
        return count


# ---------------------------------------------------------------------------
# ReportRepository：专管 reports 表，报告是审计资产，无 cap / dedup
# ---------------------------------------------------------------------------


class ReportRepository:
    """管理 reports 表的写入、检索与归档。

    报告生命周期与记忆不同：长期保留、按 id 精确检索、无淘汰策略。
    """

    def __init__(
        self,
        engine: sa.engine.Engine,
        vector_store: Any = None,
    ) -> None:
        self._engine = engine
        self._vector_store = _VectorStoreProxy(instance=vector_store)

    def init_tables(self) -> None:
        """创建 reports 表（幂等）。"""
        metadata_obj.create_all(self._engine, checkfirst=True)

    def save(
        self,
        user_id: str,
        session_id: str,
        query: str,
        analysis_type: str,
        result_json: str,
        report_markdown: str,
        anomaly_count: int,
        report_id: str | None = None,
    ) -> str:
        """写入一份分析报告，返回 report_id。"""
        if not report_id:
            report_id = str(uuid.uuid4())
        stmt = reports_table.insert().values(
            id=report_id,
            user_id=user_id,
            session_id=session_id,
            query=query,
            analysis_type=analysis_type,
            result_json=result_json,
            report_markdown=report_markdown,
            anomaly_count=anomaly_count,
            created_at=datetime.now(timezone.utc),
        )
        with self._engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

        _vs = self._vector_store.get()
        if _vs is not None:
            try:
                summary_text = report_markdown or query
                _vs.add_documents([{
                    "id": f"report_{report_id}",
                    "text": summary_text[:2000],
                    "metadata": {
                        "source": "report",
                        "report_id": report_id,
                        "user_id": user_id,
                        "session_id": session_id,
                        "analysis_type": analysis_type,
                        "anomaly_count": anomaly_count,
                    },
                }])
            except Exception as exc:  # noqa: BLE001
                _inc("ltm.vector.write.error")
                _logger.warning(
                    "ltm.vector.write.error report_id=%s user_id=%s error=%s",
                    report_id, user_id, exc,
                )

        return report_id

    def get(self, report_id: str) -> dict[str, Any] | None:
        """按 ID 获取报告，不存在返回 None。"""
        stmt = sa.select(reports_table).where(reports_table.c.id == report_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).fetchone()
            return dict(row._mapping) if row is not None else None

    def list(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """按创建时间倒序列出用户报告。"""
        stmt = (
            sa.select(reports_table)
            .where(reports_table.c.user_id == user_id)
            .order_by(reports_table.c.created_at.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def search_semantic(self, user_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """语义检索历史报告（需要 vector_store）。"""
        _vs = self._vector_store.get()
        if _vs is None:
            return []
        try:
            return _vs.search(
                query=query,
                top_k=limit,
                where={"user_id": user_id, "source": "report"},
            )
        except Exception as exc:  # noqa: BLE001
            _inc("ltm.vector.search.error")
            _logger.warning(
                "ltm.vector.search.error (reports) user_id=%s error=%s", user_id, exc
            )
            return []

    def delete_user(self, user_id: str) -> int:
        """删除指定用户的全部报告，返回删除行数。"""
        with self._engine.connect() as conn:
            res = conn.execute(reports_table.delete().where(reports_table.c.user_id == user_id))
            count = res.rowcount or 0
            conn.commit()
        _vs = self._vector_store.get()
        if _vs is not None:
            try:
                _vs.delete(where={"user_id": user_id, "source": "report"})
            except Exception as exc:  # noqa: BLE001
                _inc("ltm.vector.delete.error")
                _logger.warning(
                    "ltm.vector.delete.error (reports) user_id=%s error=%s", user_id, exc
                )
        return count

    def delete_all(self) -> int:
        """清空 reports 表全部记录，返回删除行数。"""
        with self._engine.connect() as conn:
            res = conn.execute(reports_table.delete())
            count = res.rowcount or 0
            conn.commit()
        return count


# ---------------------------------------------------------------------------
# 新单例：MemoryRepository / ReportRepository
# ---------------------------------------------------------------------------

_memory_repository_singleton: MemoryRepository | None = None
_report_repository_singleton: ReportRepository | None = None


def _make_vs_factory(settings: Any, collection: str) -> Any:
    """返回一个 VectorStore factory 函数（用于注入 _VectorStoreProxy）。

    factory 每次被调用时都会尝试从头初始化 VectorStore，
    失败时抛出异常（由 proxy 捕获并计入退避）。
    """
    def _factory() -> Any:
        from core.knowledge.vector_store import VectorStore
        vs = VectorStore.from_settings(settings, collection)
        vs.initialize()
        return vs

    return _factory


def get_memory_repository() -> MemoryRepository:
    """获取进程级 MemoryRepository 单例（懒初始化 + 自动建表）。

    向量存储通过 _VectorStoreProxy 注入，支持失败后按指数退避自动重试。
    """
    global _memory_repository_singleton
    if _memory_repository_singleton is None:
        from config.settings import get_settings
        from core.database.engine import get_engine

        settings = get_settings()

        vs_proxy: _VectorStoreProxy
        if settings.chroma.enable_long_term_indexing:
            vs_proxy = _VectorStoreProxy(factory=_make_vs_factory(settings, "memory_long_term"))
        else:
            vs_proxy = _VectorStoreProxy()  # 纯 SQL 模式

        repo = MemoryRepository.__new__(MemoryRepository)
        repo._engine = get_engine(settings.postgresql)
        repo._vector_store = vs_proxy
        repo._fusion_k = settings.memory.long_term_fusion_k
        repo._max_per_user = settings.memory.long_term_max_per_user
        repo._min_content_len = settings.memory.long_term_min_content_len
        repo._dedupe_window_seconds = settings.memory.long_term_dedupe_window_seconds
        repo._skip_empty_conclusions = settings.memory.long_term_skip_empty_conclusions
        repo.init_tables()
        _memory_repository_singleton = repo
    return _memory_repository_singleton


def get_report_repository() -> ReportRepository:
    """获取进程级 ReportRepository 单例（懒初始化 + 自动建表）。

    向量存储通过 _VectorStoreProxy 注入，支持失败后按指数退避自动重试。
    """
    global _report_repository_singleton
    if _report_repository_singleton is None:
        from config.settings import get_settings
        from core.database.engine import get_engine

        settings = get_settings()

        vs_proxy: _VectorStoreProxy
        if settings.chroma.enable_report_indexing:
            vs_proxy = _VectorStoreProxy(factory=_make_vs_factory(settings, "memory_long_term"))
        else:
            vs_proxy = _VectorStoreProxy()  # 纯 SQL 模式

        repo = ReportRepository.__new__(ReportRepository)
        repo._engine = get_engine(settings.postgresql)
        repo._vector_store = vs_proxy
        repo.init_tables()
        _report_repository_singleton = repo
    return _report_repository_singleton


def reset_repositories() -> None:
    """重置两个新单例。仅供测试使用。"""
    global _memory_repository_singleton, _report_repository_singleton
    _memory_repository_singleton = None
    _report_repository_singleton = None
