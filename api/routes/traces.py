"""Trace 查询路由：列表 / 详情 / 聚合统计。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from api.schemas.trace import IoSpanOut, RunDetailOut, RunOut, SpanOut, StatRow
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
    # 批量获取 token_summary，消除 N+1（50 条记录从 51 次查询降为 2 次）
    summaries = store.batch_get_token_summaries([r.trace_id for r in runs])
    result: list[RunOut] = []
    for r in runs:
        out = RunOut.model_validate(r)
        out.token_summary = summaries.get(r.trace_id)
        result.append(out)
    return result


@router.get("/stats", response_model=list[StatRow])
def trace_stats(
    group_by: str = Query(default="tool_name", pattern="^(tool_name|span_type)$"),
    since: datetime | None = Query(default=None),
) -> list[StatRow]:
    store = _store_or_503()
    return [StatRow(**row) for row in store.stats(group_by=group_by, since=since)]


@router.get("/{trace_id}/io", response_model=list[IoSpanOut])
def get_trace_io(
    trace_id: str,
    span_type: str | None = Query(default=None, pattern="^(model|tool)$"),
) -> list[IoSpanOut]:
    """查询某次 trace 的 model / tool 输入输出记录。

    可通过 `span_type` 过滤只看模型调用或工具调用。
    """
    store = _store_or_503()
    found = store.get_run(trace_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} not found")
    _, spans = found
    out: list[IoSpanOut] = []
    for s in spans:
        if s.span_type not in ("model", "tool"):
            continue
        if span_type and s.span_type != span_type:
            continue
        attrs = s.attributes or {}
        out.append(
            IoSpanOut(
                span_id=s.span_id,
                trace_id=s.trace_id,
                span_type=s.span_type,
                name=s.name,
                status=s.status,
                started_at=s.started_at,
                duration_ms=s.duration_ms,
                input=attrs.get("input"),
                output=attrs.get("output"),
                error=s.error,
            )
        )
    return out


@router.get("/{trace_id}", response_model=RunDetailOut)
def get_trace(trace_id: str) -> RunDetailOut:
    store = _store_or_503()
    found = store.get_run(trace_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} not found")
    run, spans = found
    out = RunDetailOut(
        **RunOut.model_validate(run).model_dump(),
        spans=[SpanOut.model_validate(s) for s in spans],
    )
    out.token_summary = store.get_token_summary(trace_id)
    return out
