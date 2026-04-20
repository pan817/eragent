"""ETL Admin API routes.

Provides endpoints for monitoring ETL sync status, manually triggering
syncs (async), and viewing metrics/traces.
Mounted under ``/api/v1/ptp-agent/admin/etl``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/admin/etl", tags=["ETL Admin"])


class TriggerRequest(BaseModel):
    """Request body for manual ETL sync trigger."""

    sync_type: str = Field(
        default="incremental",
        description="Sync type: 'full' or 'incremental'",
    )


def _get_scheduler(request: Request) -> Any:
    scheduler = getattr(request.app.state, "etl_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="ETL is not enabled")
    return scheduler


@router.get("/status")
async def get_etl_status(request: Request) -> list[dict[str, Any]]:
    """Return sync status for all ETL tables."""
    scheduler = _get_scheduler(request)
    return await scheduler._state.get_all_states()


@router.post("/trigger")
async def trigger_etl_sync(
    body: TriggerRequest, request: Request
) -> dict[str, Any]:
    """Submit an ETL sync as a background task. Returns immediately."""
    scheduler = _get_scheduler(request)
    if body.sync_type not in ("full", "incremental"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sync_type: {body.sync_type}. Use 'full' or 'incremental'.",
        )
    result = await scheduler.trigger_manual_sync(body.sync_type)
    return result


@router.get("/sync-status")
async def get_sync_status(request: Request) -> dict[str, Any]:
    """Return the status of the current/last manual sync."""
    scheduler = _get_scheduler(request)
    return scheduler.get_sync_status()


@router.get("/metrics")
async def get_etl_metrics() -> dict[str, Any]:
    """Return ETL metrics snapshot."""
    from core.etl.metrics import etl_metrics

    return etl_metrics.snapshot()


@router.get("/traces")
async def get_etl_traces() -> list[dict[str, Any]]:
    """Return recent ETL sync traces."""
    from core.etl.tracing import etl_tracer

    return etl_tracer.get_recent_traces()
