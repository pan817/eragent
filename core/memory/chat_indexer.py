"""ChatHistoryIndexer — chat 消息异步向量索引。

MemoryManager 内部组件，不向 Orchestrator 暴露。
职责：消息入队 → 对话片段聚合 → 延迟批量 flush → embedding → Chroma 写入 → 死信表。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from config.settings import Settings
from core.time_utils import now_cn

_logger = logging.getLogger(__name__)


@dataclass
class _PendingMessage:
    session_id: str
    message_id: str
    role: str
    content: str
    user_id: str
    created_at: datetime


@dataclass
class _Fragment:
    """user→assistant 一对聚合成的对话片段。"""

    user_id: str
    session_id: str
    message_ids: list[str]
    role_pattern: str
    content: str
    created_at: datetime
    entities: dict[str, Any] = field(default_factory=dict)


class ChatHistoryIndexer:
    """chat 消息异步向量索引器。

    生命周期由 MemoryManager 管理：
    - 构造时启动 asyncio flush 定时任务
    - close() 时 flush 剩余队列并取消定时器
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cfg = settings.memory.chat_history
        self._queue: list[_PendingMessage] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._vector_store: Any = None
        self._closed = False

    # ── 公开方法 ──────────────────────────────────────────────

    def enqueue(
        self,
        session_id: str,
        message_id: str,
        role: str,
        content: str,
        user_id: str,
    ) -> None:
        """入队一条 chat 消息（fire-and-forget，不阻塞调用方）。"""
        if not self._cfg.indexing_enabled or self._closed:
            return

        self._queue.append(
            _PendingMessage(
                session_id=session_id,
                message_id=message_id,
                role=role,
                content=content,
                user_id=user_id,
                created_at=now_cn(),
            )
        )

        self._record_span_fire_forget(
            "memory.chat.index.enqueue",
            session_id=session_id,
            queue_depth=len(self._queue),
        )

        self._ensure_flush_loop()

    async def flush(self) -> int:
        """立即 flush 队列中的所有消息。返回成功写入 Chroma 的片段数。"""
        async with self._lock:
            if not self._queue:
                return 0

            batch = list(self._queue)
            self._queue.clear()

        fragments = self._aggregate_fragments(batch)
        if not fragments:
            return 0

        return await self._write_fragments(fragments)

    def close(self) -> None:
        """关闭索引器：flush 剩余队列，取消定时器。"""
        self._closed = True
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()

    # ── 内部方法 ──────────────────────────────────────────────

    def _ensure_flush_loop(self) -> None:
        """确保 flush 定时循环在运行。"""
        if self._flush_task is None or self._flush_task.done():
            try:
                self._flush_task = asyncio.create_task(
                    self._flush_loop(), name="chat_indexer_flush_loop",
                )
            except RuntimeError:
                pass

    async def _flush_loop(self) -> None:
        """定时 flush 循环。"""
        interval = self._cfg.fragment_aggregation_seconds
        while not self._closed:
            await asyncio.sleep(interval)
            try:
                await self.flush()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("chat indexer flush loop error: %s", exc)

    def _aggregate_fragments(
        self, messages: list[_PendingMessage],
    ) -> list[_Fragment]:
        """将消息按 session 分组，聚合为 user→assistant 对话片段。"""
        by_session: dict[str, list[_PendingMessage]] = defaultdict(list)
        for msg in messages:
            by_session[msg.session_id].append(msg)

        fragments: list[_Fragment] = []
        max_msgs = self._cfg.fragment_max_messages

        for session_id, session_msgs in by_session.items():
            session_msgs.sort(key=lambda m: m.created_at)

            chunk: list[_PendingMessage] = []
            for msg in session_msgs:
                chunk.append(msg)
                if len(chunk) >= max_msgs or (
                    msg.role == "assistant" and len(chunk) >= 2
                ):
                    fragments.append(self._build_fragment(session_id, chunk))
                    chunk = []

            if chunk:
                fragments.append(self._build_fragment(session_id, chunk))

        return fragments

    @staticmethod
    def _build_fragment(
        session_id: str, chunk: list[_PendingMessage],
    ) -> _Fragment:
        roles = [m.role for m in chunk]
        content_parts = [f"[{m.role}] {m.content}" for m in chunk]
        return _Fragment(
            user_id=chunk[0].user_id,
            session_id=session_id,
            message_ids=[m.message_id for m in chunk],
            role_pattern="→".join(roles),
            content="\n".join(content_parts),
            created_at=chunk[0].created_at,
        )

    async def _write_fragments(self, fragments: list[_Fragment]) -> int:
        """写入 Chroma + 死信处理。"""
        store = self._get_vector_store()
        if store is None:
            for f in fragments:
                await self._write_dead_letter(f, "vector store unavailable")
            return 0

        success_count = 0
        for fragment in fragments:
            try:
                await asyncio.to_thread(
                    self._upsert_to_chroma, store, fragment,
                )
                success_count += 1
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "chat index write failed for session=%s: %s",
                    fragment.session_id, exc,
                )
                await self._write_dead_letter(fragment, str(exc))

        self._record_span_fire_forget(
            "memory.chat.index.flush",
            fragment_count=len(fragments),
            success_count=success_count,
            failed_count=len(fragments) - success_count,
        )
        return success_count

    def _upsert_to_chroma(self, store: Any, fragment: _Fragment) -> None:
        """同步方法：写入单个片段到 Chroma（在线程池中执行）。"""
        collection = store._ensure_collection()
        doc_id = f"chat_{fragment.session_id}_{fragment.message_ids[0]}"
        metadata = {
            "user_id": fragment.user_id,
            "session_id": fragment.session_id,
            "message_ids": ",".join(fragment.message_ids),
            "role_pattern": fragment.role_pattern,
            "created_at": fragment.created_at.isoformat(),
            "record_type": "chat_fragment",
        }
        collection.upsert(
            ids=[doc_id],
            documents=[fragment.content],
            metadatas=[metadata],
        )

    async def _write_dead_letter(
        self, fragment: _Fragment, error_msg: str,
    ) -> None:
        """写入死信表。"""
        try:
            from core.database.engine import get_session
            from core.memory.tables import chat_index_dead_letter_table

            fragment_data = {
                "user_id": fragment.user_id,
                "session_id": fragment.session_id,
                "message_ids": fragment.message_ids,
                "role_pattern": fragment.role_pattern,
                "content": fragment.content,
                "created_at": fragment.created_at.isoformat(),
            }
            with get_session() as session:
                session.execute(
                    chat_index_dead_letter_table.insert().values(
                        id=str(uuid.uuid4()),
                        fragment_data=fragment_data,
                        error_message=error_msg,
                        retry_count=0,
                        created_at=now_cn(),
                    )
                )
                session.commit()

            self._record_span_fire_forget(
                "memory.chat.index.dead_letter",
                session_id=fragment.session_id,
                error=error_msg,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "failed to write dead letter for session=%s: %s",
                fragment.session_id, exc,
            )

    def _get_vector_store(self) -> Any:
        """懒初始化 Chroma VectorStore（chat_history collection）。"""
        if self._vector_store is not None:
            return self._vector_store
        try:
            from core.knowledge.vector_store import VectorStore

            store = VectorStore.from_settings(self._settings, "chat_history")
            store.initialize()
            self._vector_store = store
            return store
        except Exception as exc:  # noqa: BLE001
            _logger.warning("chat_history vector store init failed: %s", exc)
            return None

    @staticmethod
    def _record_span_fire_forget(name: str, **attrs: Any) -> None:
        """记录 memory trace span（非阻塞）。"""
        try:
            from core.observability.tracing import record_span

            with record_span("memory", name, **attrs):
                pass
        except Exception:  # noqa: BLE001
            pass
