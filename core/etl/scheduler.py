"""ETL scheduler — periodic incremental sync with optional full-sync on startup.

Integrates into the FastAPI lifespan via :meth:`start` / :meth:`shutdown`.
Manual triggers run as background tasks and return immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
from core.logging_utils import get_logger
import uuid
from typing import Any

from core.etl.pipeline import ETLPipeline
from core.etl.state import SyncStateManager

_logger = get_logger(__name__)


class ETLScheduler:
    """Periodic ETL scheduler with async manual-trigger support."""

    def __init__(
        self,
        pipeline: ETLPipeline,
        state_manager: SyncStateManager,
        interval_seconds: int = 600,
        full_sync_on_startup: bool = True,
    ) -> None:
        self._pipeline = pipeline
        self._state = state_manager
        self._interval = interval_seconds
        self._full_on_startup = full_sync_on_startup
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._lock = asyncio.Lock()
        # Track active manual sync
        self._active_sync: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Called during FastAPI lifespan startup."""
        self._running = True

        # Crash recovery: mark stale RUNNING rows as FAILED
        recovered = await self._state.recover_stale_running()
        if recovered:
            _logger.info("ETL recovered %d stale RUNNING entries", recovered)

        # Full sync on first run (if needed)
        if self._full_on_startup and await self._pipeline.needs_full_sync():
            _logger.info("ETL: first run detected, starting full sync")
            async with self._lock:
                await self._pipeline.run_full_sync()

        # Start incremental loop
        self._task = asyncio.create_task(self._loop())
        _logger.info(
            "ETL scheduler started, interval=%ds", self._interval
        )

    async def shutdown(self) -> None:
        """Gracefully stop: cancel the loop and wait for current batch."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        _logger.info("ETL scheduler stopped")

    # ------------------------------------------------------------------
    # Manual trigger (Admin API) — async background task
    # ------------------------------------------------------------------

    async def trigger_manual_sync(
        self, sync_type: str = "incremental"
    ) -> dict[str, Any]:
        """Submit a sync as a background task. Returns immediately.

        Returns:
            ``{"sync_id": "...", "sync_type": "...", "state": "running"}``
            or ``{"state": "already_running", ...}`` if a sync is in progress.
        """
        if self._active_sync and self._active_sync["state"] == "running":
            return {
                "sync_id": self._active_sync["sync_id"],
                "sync_type": self._active_sync["sync_type"],
                "state": "already_running",
            }

        sync_id = str(uuid.uuid4())[:8]
        self._active_sync = {
            "sync_id": sync_id,
            "sync_type": sync_type,
            "state": "running",
            "result": None,
        }

        asyncio.create_task(self._run_manual_sync(sync_id, sync_type))

        _logger.info("ETL manual sync submitted: sync_id=%s type=%s", sync_id, sync_type)
        return {
            "sync_id": sync_id,
            "sync_type": sync_type,
            "state": "running",
        }

    def get_sync_status(self) -> dict[str, Any]:
        """Return the status of the current/last manual sync."""
        if self._active_sync is None:
            return {"state": "idle"}
        return dict(self._active_sync)

    async def _run_manual_sync(self, sync_id: str, sync_type: str) -> None:
        """Execute a manual sync in the background."""
        async with self._lock:
            try:
                if sync_type == "full":
                    result = await self._pipeline.run_full_sync()
                else:
                    result = await self._pipeline.run_incremental_sync()

                if self._active_sync and self._active_sync["sync_id"] == sync_id:
                    self._active_sync["state"] = "completed"
                    self._active_sync["result"] = result.to_dict()

                _logger.info(
                    "ETL manual sync completed: sync_id=%s rows=%d duration_ms=%.0f",
                    sync_id,
                    result.total_rows,
                    result.duration_ms,
                )
            except Exception:
                if self._active_sync and self._active_sync["sync_id"] == sync_id:
                    self._active_sync["state"] = "failed"
                    self._active_sync["result"] = {"error": "sync failed, check logs"}
                _logger.error(
                    "ETL manual sync failed: sync_id=%s", sync_id, exc_info=True
                )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Periodic incremental sync loop."""
        while self._running:
            await asyncio.sleep(self._interval)
            if not self._running:
                break
            async with self._lock:
                try:
                    result = await self._pipeline.run_incremental_sync()
                    _logger.info(
                        "ETL incremental sync: rows=%d duration_ms=%.0f",
                        result.total_rows,
                        result.duration_ms,
                    )
                except Exception:
                    _logger.error(
                        "ETL incremental sync failed", exc_info=True
                    )
