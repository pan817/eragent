"""Trace 相关 API Pydantic schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SpanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    span_id: str
    trace_id: str
    parent_span_id: str | None
    span_type: str
    name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: float | None
    attributes: dict[str, Any] | None = None
    error: str | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trace_id: str
    agent_name: str
    session_id: str | None
    user_id: str | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: float | None
    model_call_count: int
    tool_call_count: int
    error: str | None = None


class RunDetailOut(RunOut):
    spans: list[SpanOut] = Field(default_factory=list)


class StatRow(BaseModel):
    key: str
    span_type: str
    count: int
    avg_ms: float
    p50_ms: float
    p95_ms: float
    min_ms: float
    max_ms: float
