"""Trace 查询路由：列表 / 详情 / 聚合统计。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from api.schemas.trace import RunDetailOut, RunOut, SpanOut, StatRow
from core.observability.store import get_trace_store

router = APIRouter(prefix="/traces", tags=["traces"])


def _store_or_503():
    store = get_trace_store()
    if store is None:
        raise HTTPException(status_code=503, detail="trace store not initialized")
    return store


@router.get("", response_model=list[RunOut])
def list_traces(
    session_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[RunOut]:
    store = _store_or_503()
    runs = store.list_runs(
        session_id=session_id,
        user_id=user_id,
        since=since,
        limit=limit,
        offset=offset,
    )
    return [RunOut.model_validate(r) for r in runs]


@router.get("/stats", response_model=list[StatRow])
def trace_stats(
    group_by: str = Query(default="tool_name", pattern="^(tool_name|span_type)$"),
    since: datetime | None = Query(default=None),
) -> list[StatRow]:
    store = _store_or_503()
    return [StatRow(**row) for row in store.stats(group_by=group_by, since=since)]


@router.get("/{trace_id}", response_model=RunDetailOut)
def get_trace(trace_id: str) -> RunDetailOut:
    store = _store_or_503()
    found = store.get_run(trace_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} not found")
    run, spans = found
    return RunDetailOut(
        **RunOut.model_validate(run).model_dump(),
        spans=[SpanOut.model_validate(s) for s in spans],
    )
