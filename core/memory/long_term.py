"""长期记忆：基于 PostgreSQL 的记忆和报告持久化。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy import create_engine

from core.memory.tables import memories_table, metadata_obj, reports_table


class LongTermMemory:
    """基于 PostgreSQL 的长期记忆持久化。

    使用 SQLAlchemy Core 管理两张表：
    - ``memories``：通用记忆存储，支持按用户和关键词检索；
    - ``reports``：分析报告存储，支持按用户列出和按 ID 查询。
    """

    def __init__(
        self,
        dsn: str,
        vector_store: Any = None,
    ) -> None:
        """
        Args:
            dsn: PostgreSQL DSN。
            vector_store: 可选的向量存储，用于语义召回长期记忆和报告摘要。
                为 None 时退化为纯 SQL 模式（向后兼容）。
                需要外部传入已 ``initialize()`` 完成的 VectorStore 实例。
        """
        self.dsn: str = dsn
        self.engine: sa.engine.Engine = create_engine(
            dsn,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
        self._vector_store: Any = vector_store

    def init_tables(self) -> None:
        """创建数据库表（幂等）。"""
        metadata_obj.create_all(self.engine, checkfirst=True)

    # ------------------------------------------------------------------
    # memories
    # ------------------------------------------------------------------

    def save_memory(
        self,
        user_id: str,
        session_id: str,
        memory_type: str,
        content: str,
        metadata: dict[str, Any],
    ) -> str:
        memory_id: str = str(uuid.uuid4())
        stmt = memories_table.insert().values(
            id=memory_id,
            user_id=user_id,
            session_id=session_id,
            memory_type=memory_type,
            content=content,
            metadata=metadata,
            created_at=datetime.now(timezone.utc),
        )
        with self.engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

        # 旁路：向量索引（best-effort）
        if getattr(self, "_vector_store", None) is not None:
            try:
                self._vector_store.add_documents([{
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
            except Exception:  # noqa: BLE001
                # 向量索引失败不影响 SQL 主路径
                pass

        return memory_id

    def search_memories_semantic(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """语义检索长期记忆。

        要求构造 LongTermMemory 时传入 ``vector_store``，否则返回空列表。
        通过 metadata 中的 ``user_id`` 实现用户隔离。
        """
        if getattr(self, "_vector_store", None) is None:
            return []
        try:
            hits = self._vector_store.search(
                query=query,
                top_k=limit,
                where={"user_id": user_id, "source": "memory"},
            )
        except Exception:  # noqa: BLE001
            return []
        return hits

    def search_memories(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """按 LIKE 关键词检索（MVP，后续可换向量检索）。"""
        stmt = (
            sa.select(memories_table)
            .where(
                sa.and_(
                    memories_table.c.user_id == user_id,
                    memories_table.c.content.like(f"%{query}%"),
                )
            )
            .order_by(memories_table.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            result = conn.execute(stmt)
            return [dict(row._mapping) for row in result]

    # ------------------------------------------------------------------
    # reports
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
        with self.engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

        # 旁路：把报告摘要写入向量库（用于"类似历史报告"召回）
        if getattr(self, "_vector_store", None) is not None:
            try:
                summary_text = report_markdown or query
                self._vector_store.add_documents([{
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
            except Exception:  # noqa: BLE001
                pass

        return report_id

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        stmt = sa.select(reports_table).where(reports_table.c.id == report_id)
        with self.engine.connect() as conn:
            result = conn.execute(stmt)
            row = result.fetchone()
            if row is None:
                return None
            return dict(row._mapping)

    def search_reports_semantic(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """语义检索用户的历史分析报告。

        要求构造时传入 ``vector_store``，否则返回空列表。
        """
        if getattr(self, "_vector_store", None) is None:
            return []
        try:
            return self._vector_store.search(
                query=query,
                top_k=limit,
                where={"user_id": user_id, "source": "report"},
            )
        except Exception:  # noqa: BLE001
            return []

    def list_reports(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        stmt = (
            sa.select(reports_table)
            .where(reports_table.c.user_id == user_id)
            .order_by(reports_table.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            result = conn.execute(stmt)
            return [dict(row._mapping) for row in result]

    # ------------------------------------------------------------------
    # 删除 / 重置
    # ------------------------------------------------------------------

    def delete_user_data(
        self,
        user_id: str,
        *,
        delete_memories: bool = True,
        delete_reports: bool = True,
    ) -> dict[str, int]:
        deleted = {"memories": 0, "reports": 0}
        with self.engine.connect() as conn:
            if delete_memories:
                res = conn.execute(
                    memories_table.delete().where(memories_table.c.user_id == user_id)
                )
                deleted["memories"] = res.rowcount or 0
            if delete_reports:
                res = conn.execute(
                    reports_table.delete().where(reports_table.c.user_id == user_id)
                )
                deleted["reports"] = res.rowcount or 0
            conn.commit()

        # 旁路：清理向量库中属于该用户的条目
        if getattr(self, "_vector_store", None) is not None:
            try:
                self._vector_store.delete(where={"user_id": user_id})
            except Exception:  # noqa: BLE001
                pass

        return deleted

    def delete_all(self) -> dict[str, int]:
        """清空 memories 与 reports 全部记录。仅供管理端使用。"""
        deleted = {"memories": 0, "reports": 0}
        with self.engine.connect() as conn:
            res = conn.execute(memories_table.delete())
            deleted["memories"] = res.rowcount or 0
            res = conn.execute(reports_table.delete())
            deleted["reports"] = res.rowcount or 0
            conn.commit()
        return deleted


# ---------------------------------------------------------------------------
# 模块级懒单例
# ---------------------------------------------------------------------------

_long_term_memory_singleton: LongTermMemory | None = None


def get_long_term_memory() -> LongTermMemory:
    """获取进程级 LongTermMemory 单例（懒初始化 + 自动建表）。

    若 ``settings.chroma.enable_long_term_indexing`` 或
    ``enable_report_indexing`` 为 True，则同时构造一个 VectorStore 注入，
    用于语义召回。任何向量相关初始化失败都会被吞掉，单例退化为纯 SQL 模式。
    """
    global _long_term_memory_singleton
    if _long_term_memory_singleton is None:
        from config.settings import get_settings

        settings = get_settings()
        vector_store: Any = None
        if (
            settings.chroma.enable_long_term_indexing
            or settings.chroma.enable_report_indexing
        ):
            try:
                from core.knowledge.vector_store import VectorStore

                vs = VectorStore.from_settings(settings, "memory_long_term")
                vs.initialize()
                vector_store = vs
            except Exception:  # noqa: BLE001
                vector_store = None

        ltm = LongTermMemory(
            dsn=settings.postgresql.dsn,
            vector_store=vector_store,
        )
        ltm.init_tables()
        _long_term_memory_singleton = ltm
    return _long_term_memory_singleton


def reset_long_term_memory() -> None:
    """重置单例。仅供测试使用。"""
    global _long_term_memory_singleton
    _long_term_memory_singleton = None
