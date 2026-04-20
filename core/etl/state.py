"""ETL sync state (watermark) manager.

Tracks per-table high-water marks for incremental synchronisation.
All database access runs in a thread-pool via ``asyncio.to_thread``
so callers can ``await`` without blocking the event loop.
"""

from __future__ import annotations

from core.logging_utils import get_logger
from datetime import datetime
from typing import Any

import asyncio
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from core.etl.tables import ETLSyncState
from core.time_utils import now_cn

_logger = get_logger(__name__)


class SyncStateManager:
    """Manages ETL sync watermarks persisted in ``etl_sync_state``."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Public API (all async, internally dispatched to thread)
    # ------------------------------------------------------------------

    async def get_watermark(self, table_name: str) -> datetime | None:
        """Return the last watermark for *table_name*, or ``None``."""
        return await asyncio.to_thread(self._get_watermark_sync, table_name)

    async def update_watermark(
        self,
        table_name: str,
        domain: str,
        watermark: datetime,
        rows_synced: int,
        sync_type: str,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Upsert the sync state row for *table_name*."""
        await asyncio.to_thread(
            self._update_watermark_sync,
            table_name,
            domain,
            watermark,
            rows_synced,
            sync_type,
            status,
            error_message,
        )

    async def needs_full_sync(self, table_names: list[str]) -> bool:
        """Return ``True`` if any table in *table_names* has no watermark."""
        return await asyncio.to_thread(self._needs_full_sync_sync, table_names)

    async def get_all_states(self) -> list[dict[str, Any]]:
        """Return all sync-state rows as dicts (Admin API)."""
        return await asyncio.to_thread(self._get_all_states_sync)

    async def recover_stale_running(self) -> int:
        """Mark RUNNING rows as FAILED (crash recovery). Return count."""
        return await asyncio.to_thread(self._recover_stale_running_sync)

    # ------------------------------------------------------------------
    # Sync helpers (executed inside thread-pool)
    # ------------------------------------------------------------------

    def _get_watermark_sync(self, table_name: str) -> datetime | None:
        with self._session_factory() as session:
            row = session.execute(
                select(ETLSyncState.last_watermark).where(
                    ETLSyncState.table_name == table_name
                )
            ).scalar()
            return row

    def _update_watermark_sync(
        self,
        table_name: str,
        domain: str,
        watermark: datetime,
        rows_synced: int,
        sync_type: str,
        status: str,
        error_message: str | None,
    ) -> None:
        now = now_cn()
        with self._session_factory() as session:
            existing = session.execute(
                select(ETLSyncState).where(
                    ETLSyncState.table_name == table_name
                )
            ).scalar_one_or_none()

            if existing is None:
                row = ETLSyncState(
                    table_name=table_name,
                    domain=domain,
                    last_sync_at=now,
                    last_watermark=watermark,
                    last_sync_status=status,
                    rows_synced=rows_synced,
                    total_rows_synced=rows_synced,
                    error_message=error_message,
                    sync_type=sync_type,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                session.execute(
                    update(ETLSyncState)
                    .where(ETLSyncState.table_name == table_name)
                    .values(
                        domain=domain,
                        last_sync_at=now,
                        last_watermark=watermark,
                        last_sync_status=status,
                        rows_synced=rows_synced,
                        total_rows_synced=ETLSyncState.total_rows_synced + rows_synced,
                        error_message=error_message,
                        sync_type=sync_type,
                        updated_at=now,
                    )
                )
            session.commit()
            _logger.info(
                "ETL watermark updated: %s status=%s rows=%d",
                table_name,
                status,
                rows_synced,
            )

    def _needs_full_sync_sync(self, table_names: list[str]) -> bool:
        with self._session_factory() as session:
            existing = set(
                session.execute(
                    select(ETLSyncState.table_name).where(
                        ETLSyncState.table_name.in_(table_names)
                    )
                ).scalars().all()
            )
            missing = set(table_names) - existing
            if missing:
                _logger.info("ETL tables need full sync: %s", sorted(missing))
                return True
            return False

    def _get_all_states_sync(self) -> list[dict[str, Any]]:
        now = now_cn()
        with self._session_factory() as session:
            rows = session.execute(
                select(ETLSyncState).order_by(ETLSyncState.domain, ETLSyncState.table_name)
            ).scalars().all()
            return [
                {
                    "table_name": r.table_name,
                    "domain": r.domain,
                    "last_sync_at": r.last_sync_at.isoformat() if r.last_sync_at else None,
                    "last_watermark": r.last_watermark.isoformat() if r.last_watermark else None,
                    "last_sync_status": r.last_sync_status,
                    "rows_synced": r.rows_synced,
                    "total_rows_synced": r.total_rows_synced,
                    "sync_type": r.sync_type,
                    "error_message": r.error_message,
                    "lag_seconds": self._calc_lag(now, r.last_watermark),
                }
                for r in rows
            ]

    @staticmethod
    def _calc_lag(now: datetime, watermark: datetime | None) -> float | None:
        if watermark is None:
            return None
        # Normalise: strip tzinfo if one side is naive (SQLite drops tz)
        if now.tzinfo is not None and watermark.tzinfo is None:
            now = now.replace(tzinfo=None)
        elif now.tzinfo is None and watermark.tzinfo is not None:
            watermark = watermark.replace(tzinfo=None)
        return (now - watermark).total_seconds()

    def _recover_stale_running_sync(self) -> int:
        now = now_cn()
        with self._session_factory() as session:
            result = session.execute(
                update(ETLSyncState)
                .where(ETLSyncState.last_sync_status == "RUNNING")
                .values(
                    last_sync_status="FAILED",
                    error_message="Recovered from stale RUNNING state on startup",
                    updated_at=now,
                )
            )
            session.commit()
            count = result.rowcount  # type: ignore[union-attr]
            if count:
                _logger.warning("ETL recovered %d stale RUNNING rows", count)
            return count
