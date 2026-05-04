"""Idle session watcher — 定期扫描不活跃会话并触发 SESSION_RECAP 抽取。

由 app lifespan 启动为后台 asyncio.Task。
与 TaskRegistry sweep 解耦，职责单一：只关心会话摘要触发。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.settings import Settings
from core.time_utils import now_cn

_logger = logging.getLogger(__name__)


class IdleSessionWatcher:
    """后台会话闲置扫描器。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cfg = settings.memory.session_recap
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.create_task(
                    self._loop(), name="idle_session_watcher",
                )
            except RuntimeError:
                _logger.warning("idle watcher start failed: no running event loop")

    async def shutdown(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        interval = self._cfg.watcher_interval_seconds
        while not self._closed:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                await self._scan_once()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("idle session watcher scan failed: %s", exc)

    async def _scan_once(self) -> None:
        idle_sessions = await asyncio.to_thread(self._find_idle_sessions)
        if not idle_sessions:
            return

        _logger.info("idle watcher found %d idle sessions", len(idle_sessions))

        from core.memory.manager import MemoryManager

        manager = MemoryManager(settings=self._settings)
        try:
            for session_id, user_id in idle_sessions:
                try:
                    await manager.on_session_idle(session_id, user_id)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "session recap failed for session=%s: %s",
                        session_id, exc,
                    )
        finally:
            manager.close()

    def _find_idle_sessions(self) -> list[tuple[str, str]]:
        """查询 chat_sessions 中不活跃且未摘要的会话。"""
        from datetime import timedelta

        import sqlalchemy as sa

        from core.chat.tables import chat_sessions_table
        from core.database.engine import get_engine
        from core.memory.tables import session_summaries_table

        threshold = now_cn() - timedelta(seconds=self._cfg.idle_threshold_seconds)

        try:
            engine = get_engine(self._settings.postgresql)
            with engine.connect() as conn:
                already_summarized = sa.select(
                    session_summaries_table.c.session_id,
                )
                stmt = (
                    sa.select(
                        chat_sessions_table.c.id,
                        chat_sessions_table.c.user_id,
                    )
                    .where(
                        sa.and_(
                            chat_sessions_table.c.updated_at <= threshold,
                            chat_sessions_table.c.deleted_at.is_(None),
                            chat_sessions_table.c.message_count >= self._cfg.min_messages,
                            chat_sessions_table.c.id.notin_(already_summarized),
                        )
                    )
                    .order_by(chat_sessions_table.c.updated_at.desc())
                    .limit(10)
                )
                rows = conn.execute(stmt).fetchall()
                return [(r.id, r.user_id) for r in rows]
        except Exception as exc:  # noqa: BLE001
            _logger.warning("idle session scan query failed: %s", exc)
            return []
