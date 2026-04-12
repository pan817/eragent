"""TraceStore：后台批量落库 + 查询接口。

采用独立守护线程 + 内存队列，避免 Agent 主流程因写库阻塞。
进程退出时通过 atexit / close() 执行 flush，保证数据落地。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, sessionmaker

from core.logging_utils import get_logger
from core.observability.tables import TraceRun, TraceSpan

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 内存事件结构（Middleware -> 队列）
# ---------------------------------------------------------------------------


@dataclass
class RunEvent:
    """Agent run 开始或结束事件。"""

    kind: str  # "run_start" | "run_end"
    trace_id: str
    agent_name: str = ""
    session_id: str | None = None
    user_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    status: str = "running"
    error: str | None = None
    model_call_count: int = 0
    tool_call_count: int = 0


@dataclass
class SpanEvent:
    """单个 span 完成事件。"""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    span_type: str
    name: str
    status: str
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------
# TraceStore
# ---------------------------------------------------------------------------


class TraceStore:
    """跨线程安全的 trace 持久化组件。

    调用方（Middleware）通过 enqueue_run_* / enqueue_span 投递事件，
    后台线程批量消费并写入 PostgreSQL。
    """

    # 关闭信号：worker 收到此对象后 flush 并退出
    _SHUTDOWN_SIGNAL = object()

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        batch_size: int = 50,
        flush_interval: float = 1.0,
        queue_maxsize: int = 10000,
    ) -> None:
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_maxsize)
        self._worker: threading.Thread | None = None
        self._stopped = threading.Event()
        self._dropped_count: int = 0
        self._dropped_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None:
            return
        self._stopped.clear()
        t = threading.Thread(target=self._run, name="trace-store-worker", daemon=True)
        t.start()
        self._worker = t

    def close(self, timeout: float = 5.0) -> None:
        if self._worker is None:
            return
        self._stopped.set()
        try:
            self._queue.put_nowait(self._SHUTDOWN_SIGNAL)
        except queue.Full:
            pass
        self._worker.join(timeout=timeout)
        self._worker = None

    # ------------------------------------------------------------------
    # 入队接口（Middleware 使用）
    # ------------------------------------------------------------------

    def enqueue(self, event: RunEvent | SpanEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # 队列溢出降级丢弃，但累计计数 + 周期性告警，避免静默丢数据
            with self._dropped_lock:
                self._dropped_count += 1
                dropped = self._dropped_count
            if dropped == 1 or dropped % 100 == 0:
                _logger.warning(
                    "trace-store queue full, dropped %d events so far", dropped
                )

    @property
    def dropped_count(self) -> int:
        """累计丢弃事件数（监控用）。"""
        with self._dropped_lock:
            return self._dropped_count

    # ------------------------------------------------------------------
    # 后台消费
    # ------------------------------------------------------------------

    def _run(self) -> None:
        buffer: list[RunEvent | SpanEvent] = []
        last_flush = time.monotonic()
        while True:
            timeout = max(0.0, self._flush_interval - (time.monotonic() - last_flush))
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None

            if item is self._SHUTDOWN_SIGNAL:
                if buffer:
                    self._flush(buffer)
                # 继续消费直到 queue 空
                while True:
                    try:
                        rest = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if rest is self._SHUTDOWN_SIGNAL:
                        continue
                    buffer.append(rest)
                if buffer:
                    self._flush(buffer)
                return

            if item is not None:
                buffer.append(item)

            if len(buffer) >= self._batch_size or (
                buffer and (time.monotonic() - last_flush) >= self._flush_interval
            ):
                self._flush(buffer)
                buffer = []
                last_flush = time.monotonic()

    def _flush(self, events: list[RunEvent | SpanEvent]) -> None:
        if not events:
            return
        try:
            with self._session_factory() as session:
                for ev in events:
                    self._apply(session, ev)
                session.commit()
        except Exception as exc:  # noqa: BLE001
            _logger.error("trace-store flush failed: %s", exc, exc_info=True)

    def _apply(self, session: Session, ev: RunEvent | SpanEvent) -> None:
        if isinstance(ev, RunEvent):
            if ev.kind == "run_start":
                session.merge(
                    TraceRun(
                        trace_id=ev.trace_id,
                        agent_name=ev.agent_name,
                        session_id=ev.session_id,
                        user_id=ev.user_id,
                        status="running",
                        started_at=ev.started_at or datetime.utcnow(),
                    )
                )
            else:  # run_end
                run = session.get(TraceRun, ev.trace_id)
                if run is None:
                    run = TraceRun(
                        trace_id=ev.trace_id,
                        agent_name=ev.agent_name,
                        session_id=ev.session_id,
                        user_id=ev.user_id,
                        started_at=ev.started_at or ev.finished_at or datetime.utcnow(),
                    )
                    session.add(run)
                run.status = ev.status
                run.finished_at = ev.finished_at
                run.duration_ms = ev.duration_ms
                run.error = ev.error
                run.model_call_count = ev.model_call_count
                run.tool_call_count = ev.tool_call_count
        else:
            session.merge(
                TraceSpan(
                    span_id=ev.span_id,
                    trace_id=ev.trace_id,
                    parent_span_id=ev.parent_span_id,
                    span_type=ev.span_type,
                    name=ev.name,
                    status=ev.status,
                    started_at=ev.started_at,
                    finished_at=ev.finished_at,
                    duration_ms=ev.duration_ms,
                    attributes=ev.attributes or None,
                    error=ev.error,
                )
            )

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def list_runs(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TraceRun]:
        with self._session_factory() as session:
            stmt = select(TraceRun)
            if session_id:
                stmt = stmt.where(TraceRun.session_id == session_id)
            if user_id:
                stmt = stmt.where(TraceRun.user_id == user_id)
            if since:
                stmt = stmt.where(TraceRun.started_at >= since)
            stmt = stmt.order_by(desc(TraceRun.started_at)).limit(limit).offset(offset)
            return list(session.execute(stmt).scalars().all())

    def get_run(self, trace_id: str) -> tuple[TraceRun, list[TraceSpan]] | None:
        with self._session_factory() as session:
            run = session.get(TraceRun, trace_id)
            if run is None:
                return None
            spans = list(
                session.execute(
                    select(TraceSpan)
                    .where(TraceSpan.trace_id == trace_id)
                    .order_by(TraceSpan.started_at)
                )
                .scalars()
                .all()
            )
            return run, spans

    def get_token_summary(self, trace_id: str) -> dict[str, Any] | None:
        """从 agent span 的 attributes 中提取 token_summary。"""
        with self._session_factory() as session:
            stmt = (
                select(TraceSpan.attributes)
                .where(TraceSpan.trace_id == trace_id)
                .where(TraceSpan.span_type == "agent")
                .limit(1)
            )
            row = session.execute(stmt).scalar_one_or_none()
            if row and isinstance(row, dict):
                return row.get("token_summary")
            return None

    def stats(
        self,
        *,
        group_by: str = "tool_name",
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """按 span_type / name 聚合统计 count、avg、p50、p95、max。"""
        with self._session_factory() as session:
            if group_by == "span_type":
                group_col = TraceSpan.span_type
            else:  # tool_name / name
                group_col = TraceSpan.name

            stmt = select(
                group_col.label("key"),
                TraceSpan.span_type.label("span_type"),
                func.count().label("count"),
                func.avg(TraceSpan.duration_ms).label("avg_ms"),
                func.max(TraceSpan.duration_ms).label("max_ms"),
                func.min(TraceSpan.duration_ms).label("min_ms"),
                func.percentile_cont(0.5)
                .within_group(TraceSpan.duration_ms)
                .label("p50_ms"),
                func.percentile_cont(0.95)
                .within_group(TraceSpan.duration_ms)
                .label("p95_ms"),
            ).where(TraceSpan.duration_ms.is_not(None))
            if since:
                stmt = stmt.where(TraceSpan.started_at >= since)
            stmt = stmt.group_by(group_col, TraceSpan.span_type).order_by(
                desc("count")
            )

            rows = session.execute(stmt).all()
            return [
                {
                    "key": r.key,
                    "span_type": r.span_type,
                    "count": int(r.count),
                    "avg_ms": float(r.avg_ms or 0),
                    "p50_ms": float(r.p50_ms or 0),
                    "p95_ms": float(r.p95_ms or 0),
                    "min_ms": float(r.min_ms or 0),
                    "max_ms": float(r.max_ms or 0),
                }
                for r in rows
            ]


# ---------------------------------------------------------------------------
# 全局单例（FastAPI 启动时初始化，middleware / 路由共享）
# ---------------------------------------------------------------------------


_store: TraceStore | None = None


def init_trace_store(
    session_factory: sessionmaker[Session],
    **kwargs: Any,
) -> TraceStore:
    """初始化全局 TraceStore（幂等）。"""
    global _store
    if _store is None:
        _store = TraceStore(session_factory, **kwargs)
        _store.start()
    return _store


def get_trace_store() -> TraceStore | None:
    return _store


def shutdown_trace_store() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None
