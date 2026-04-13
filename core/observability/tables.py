"""可观测性持久化模型：trace_runs / trace_spans。

trace_runs 记录一次 Agent 调用的整体信息，trace_spans 记录其中每个
agent/model/tool span 的耗时、父子关系与属性。两张表通过 trace_id 关联。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.models import Base


class TraceRun(Base):
    """一次 Agent 调用的顶层记录。"""

    __tablename__ = "trace_runs"

    trace_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    spans: Mapped[list["TraceSpan"]] = relationship(
        "TraceSpan",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="TraceSpan.started_at",
    )

    __table_args__ = (
        Index("ix_trace_runs_started_at_desc", started_at.desc()),
        Index("ix_trace_runs_session_started", "session_id", started_at.desc()),
        Index("ix_trace_runs_user_started", "user_id", started_at.desc()),
    )


class TraceSpan(Base):
    """单个调用 span：agent / model / tool。"""

    __tablename__ = "trace_spans"

    span_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trace_runs.trace_id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_span_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    span_type: Mapped[str] = mapped_column(String(16), nullable=False)  # agent|model|tool
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    attributes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[TraceRun] = relationship("TraceRun", back_populates="spans")

    __table_args__ = (
        Index("ix_trace_spans_trace_started", "trace_id", "started_at"),
        Index("ix_trace_spans_type_name", "span_type", "name"),
        Index("ix_trace_spans_trace_type", "trace_id", "span_type"),
    )
