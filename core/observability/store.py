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
from core.time_utils import now_cn

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
        # run_end flush 同步屏障：
        # - 调用方（registry._run）在 finish_run 入队后调 flush_now_sync(trace_id)
        #   同步等待该 trace 的 run_end 被 commit 到 DB，再 publish_done 给前端
        # - 解决的 race：前端收到 done 后立刻查 /tasks/{id}，快照走到其他 worker
        #   回落 trace_runs 但还显示 running
        self._flush_barrier_lock = threading.Lock()
        self._run_end_waiters: dict[str, threading.Event] = {}
        # 已 flush 但调用方还没来拿的 trace_id 集合（处理"flush 先于 wait"的时序）。
        # 被消费后立即 discard；留一个 soft cap 防极端场景泄漏。
        self._run_end_flushed: set[str] = set()
        self._run_end_flushed_cap = 1024

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
            event_desc = (
                f"span({getattr(event, 'span_type', '?')}/{getattr(event, 'name', '?')})"
                if isinstance(event, SpanEvent)
                else f"run({getattr(event, 'trace_id', '?')})"
            )
            if dropped == 1 or dropped % 100 == 0:
                _logger.warning(
                    "trace-store queue full, dropped %d events so far "
                    "(latest: %s, error=%s)",
                    dropped,
                    event_desc,
                    getattr(event, "error", None) or "none",
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
        # 收集本批里 run_end 事件的 trace_id，commit 完成后统一唤醒等待者。
        # 这里只记录 trace_id；commit 失败时也会 signal（避免 registry 永远 hang），
        # 由 commit 自身的异常日志暴露问题。
        run_end_trace_ids = {
            ev.trace_id for ev in events
            if isinstance(ev, RunEvent) and ev.kind == "run_end"
        }
        try:
            with self._session_factory() as session:
                for ev in events:
                    self._apply(session, ev)
                session.commit()
        except Exception as exc:  # noqa: BLE001
            _logger.error("trace-store flush failed: %s", exc, exc_info=True)
        finally:
            if run_end_trace_ids:
                self._signal_run_end_flushed(run_end_trace_ids)

    def _signal_run_end_flushed(self, trace_ids: set[str]) -> None:
        """唤醒等待这些 trace 的 run_end flush 的调用方。"""
        to_set: list[threading.Event] = []
        with self._flush_barrier_lock:
            for tid in trace_ids:
                ev = self._run_end_waiters.pop(tid, None)
                if ev is not None:
                    to_set.append(ev)
                else:
                    # 调用方还没到（flush 先于 wait），记一笔，等它来了立即返回
                    self._run_end_flushed.add(tid)
            # soft cap：极端情况下（finish_run 有 run_end 入队但没人 wait），
            # 防止集合无限膨胀
            if len(self._run_end_flushed) > self._run_end_flushed_cap:
                self._run_end_flushed.clear()
        for ev in to_set:
            ev.set()

    # ------------------------------------------------------------------
    # 同步 flush 屏障（供 registry._run 在 publish_done 前调用）
    # ------------------------------------------------------------------

    def flush_now_sync(self, trace_id: str, *, timeout: float = 2.0) -> bool:
        """阻塞等待 ``trace_id`` 对应的 ``run_end`` 事件被 commit 到 DB。

        **必须在 ``finish_run`` 已经把 run_end 入队之后调用**，否则会等到
        timeout。调用方（registry._run）按如下时序使用：

        1. orchestrator.analyze() 的 finally 里 ``timing_middleware.finish_run()``
           把 run_end enqueue 到本 store
        2. registry._run 的 finally 里调 ``store.flush_now_sync(trace_id)``
        3. wait 返回后再 ``_publish_done`` + ``bus.close`` → SSE 前端收到 done
           时 DB 已终态

        返回：
            ``True`` 表示已 commit（或 commit 已在 wait 之前发生）；
            ``False`` 表示 timeout（DB 可能依然 stale，调用方应降级为 WARNING
            但仍然发 done，避免 SSE 吊死）。
        """
        with self._flush_barrier_lock:
            # 快路径：flush 早于本次 wait 调用
            if trace_id in self._run_end_flushed:
                self._run_end_flushed.discard(trace_id)
                return True
            # 慢路径：注册一个 event，等 _flush 唤醒
            waiter = self._run_end_waiters.get(trace_id)
            if waiter is None:
                waiter = threading.Event()
                self._run_end_waiters[trace_id] = waiter

        signaled = waiter.wait(timeout)

        # 清理：无论是否 signaled 都移除 waiter，避免累积
        with self._flush_barrier_lock:
            current = self._run_end_waiters.get(trace_id)
            if current is waiter:
                self._run_end_waiters.pop(trace_id, None)
            self._run_end_flushed.discard(trace_id)
        return signaled

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
                        started_at=ev.started_at or now_cn(),
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
                        started_at=ev.started_at or ev.finished_at or now_cn(),
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
        """从 agent span 的 attributes 中提取 token_summary（单条查询）。"""
        with self._session_factory() as session:
            return self._fetch_token_summary_single(session, trace_id)

    def batch_get_token_summaries(
        self, trace_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """批量获取多条 trace 的 token_summary，消除 N+1 查询。

        Args:
            trace_ids: 需要查询的 trace_id 列表。

        Returns:
            {trace_id: token_summary_dict} 映射，无 summary 的 trace 不包含在内。
        """
        if not trace_ids:
            return {}
        with self._session_factory() as session:
            stmt = (
                select(TraceSpan.trace_id, TraceSpan.attributes)
                .where(TraceSpan.trace_id.in_(trace_ids))
                .where(TraceSpan.span_type == "agent")
                .distinct(TraceSpan.trace_id)
            )
            rows = session.execute(stmt).all()
            result: dict[str, dict[str, Any]] = {}
            for trace_id, attrs in rows:
                if attrs and isinstance(attrs, dict):
                    ts = attrs.get("token_summary")
                    if ts is not None:
                        result[trace_id] = ts
            return result

    @staticmethod
    def _fetch_token_summary_single(
        session: Session, trace_id: str
    ) -> dict[str, Any] | None:
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
