"""TaskRegistry：进程内异步分析任务的生命周期管理。

- 每个任务对应一个 ``asyncio.Task``，由 ``submit()`` 创建
- 通过 ``asyncio.Semaphore`` 控制并发上限，超过即排队（状态保持 queued）
- 终态（ok/error/aborted）的 entry 在内存中保留 ``result_cache_ttl_sec``
  以便前端 GET 快照拿到完整 ``AnalysisResult``；超时后由 sweep 清理
- 不做跨进程持久化；持久化由既有 ``trace_runs`` / ``chat_messages`` 承担
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from api.schemas.analysis import AnalysisRequest
from api.schemas.domain import (
    AnalysisResult,
    AnalysisStatus,
    ErrorInfo,
)
from core.chat.tables import chat_messages_table
from core.logging_utils import get_logger
from core.observability.tables import TraceRun
from core.tasks.events import EventBusProtocol
from core.tasks.schemas import TERMINAL_STATES, TaskState
from core.time_utils import now_cn

_logger = get_logger(__name__)


RunnerFactory = Callable[["TaskEntry"], Awaitable[AnalysisResult]]
"""把 TaskEntry 转化为可运行协程的工厂；由 API 路由注入具体实现。"""

Finalizer = Callable[["TaskEntry"], Awaitable[None]]
"""任务终态收尾回调：由 API 路由注入，在所有终止路径里统一更新 chat_messages
等业务副作用表（success/error/abort/timeout/stall 全覆盖）。registry 不直接依赖
chat_repository，仅通过此回调解耦。"""


@dataclass
class TaskEntry:
    """单个异步分析任务的全部运行期信息。"""

    trace_id: str
    user_id: str
    session_id: str
    request: AnalysisRequest
    state: TaskState = TaskState.QUEUED
    created_at: datetime = field(default_factory=now_cn)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    stage: str | None = None
    assistant_message_id: str | None = None
    user_message_id: str | None = None
    result: AnalysisResult | None = None
    error: ErrorInfo | None = None
    task: asyncio.Task[Any] | None = None
    finalizer: Finalizer | None = None
    finalized: bool = False


class TaskRegistry:
    """进程内任务注册表。"""

    def __init__(
        self,
        *,
        event_bus: EventBusProtocol,
        session_factory: sessionmaker[Session],
        max_concurrent_tasks: int = 5,
        result_cache_ttl_sec: int = 600,
        sweep_interval_sec: int = 60,
        trace_flush_barrier_timeout: float = 2.0,
        runner_hard_timeout_seconds: float = 600.0,
        runner_stall_grace_seconds: float = 60.0,
        orphan_pending_chat_max_age_sec: int = 1800,
    ) -> None:
        self._bus = event_bus
        self._session_factory = session_factory
        self._ttl = result_cache_ttl_sec
        self._sweep_interval = sweep_interval_sec
        # publish_done 前等 trace_runs 落库的最长阻塞时间。
        # 正常 DB 写入 10-50ms；超时就降级 WARNING，不阻塞 SSE。
        self._trace_flush_barrier_timeout = trace_flush_barrier_timeout
        # runner 协程硬超时 + 僵尸纠偏宽限：防止 entry.state 卡 RUNNING。
        # wait_for 是第一道闸；sweep 扫 started_at 超龄的 RUNNING entry 是第二道闸
        # （覆盖 wait_for 自身失灵或被屏蔽的边缘场景）。
        self._runner_hard_timeout = runner_hard_timeout_seconds
        self._runner_stall_grace = runner_stall_grace_seconds
        # 孤儿 pending chat_messages 纠偏阈值：finalizer 是第一道闸，
        # _mark_stale_as_aborted(启动/关闭) 是第二道闸，本周期性 sweep 是第三道闸，
        # 覆盖"进程存活但 finalizer 全链路失败"的极端尾部场景。默认 30 分钟。
        stall_ceiling = runner_hard_timeout_seconds + runner_stall_grace_seconds
        if orphan_pending_chat_max_age_sec < stall_ceiling:
            safe_value = int(stall_ceiling * 2)
            _logger.warning(
                "orphan_pending_chat_max_age_sec(%d) < runner_hard_timeout(%.0f) "
                "+ stall_grace(%.0f) = %.0f; auto-corrected to %d to avoid "
                "killing chat_messages of still-running tasks",
                orphan_pending_chat_max_age_sec,
                runner_hard_timeout_seconds,
                runner_stall_grace_seconds,
                stall_ceiling,
                safe_value,
            )
            orphan_pending_chat_max_age_sec = safe_value
        self._orphan_pending_chat_max_age = orphan_pending_chat_max_age_sec
        self._stalled_count = 0
        self._max_concurrent_tasks = max_concurrent_tasks
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._entries: dict[str, TaskEntry] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[Any] | None = None
        self._closed = False
        _logger.info(
            "TaskRegistry initialized: max_concurrent=%d, "
            "result_cache_ttl=%ds, sweep_interval=%ds, "
            "runner_hard_timeout=%.0fs, stall_grace=%.0fs, "
            "orphan_pending_max_age=%ds (stall_ceiling=%.0fs)",
            max_concurrent_tasks,
            result_cache_ttl_sec,
            sweep_interval_sec,
            runner_hard_timeout_seconds,
            runner_stall_grace_seconds,
            orphan_pending_chat_max_age_sec,
            stall_ceiling,
        )

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------

    def start_background(self) -> None:
        """启动后台 sweep 协程（需在事件循环内调用）。"""
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(
                self._sweep_loop(), name="task-registry-sweep"
            )

    async def shutdown(self, timeout: float = 10.0) -> None:
        """cancel 所有在跑任务，把 trace_runs 残留标记 aborted。"""
        self._closed = True
        if self._sweeper is not None and not self._sweeper.done():
            self._sweeper.cancel()
            try:
                await self._sweeper
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._sweeper = None

        async with self._lock:
            pending = [
                e for e in self._entries.values()
                if e.task is not None and not e.task.done()
            ]
        for entry in pending:
            if entry.task is not None:
                entry.task.cancel()
        if pending:
            await asyncio.gather(
                *[entry.task for entry in pending if entry.task is not None],
                return_exceptions=True,
            )
        # 兜底：数据库层残留清理
        await asyncio.to_thread(self._mark_stale_as_aborted)

    def recover_on_startup(self) -> dict[str, int]:
        """启动时扫描 trace_runs 与 chat_messages，把残留的 running / pending
        标记为 aborted / error。返回受影响行数字典（供监控）。"""
        return self._mark_stale_as_aborted()

    # ------------------------------------------------------------------
    # 任务提交与查询
    # ------------------------------------------------------------------

    async def submit(
        self,
        request: AnalysisRequest,
        *,
        trace_id: str,
        assistant_message_id: str | None,
        user_message_id: str | None,
        runner_factory: RunnerFactory,
        finalizer: Finalizer | None = None,
    ) -> TaskEntry:
        """登记并启动一个异步分析任务。

        runner_factory 负责调用 orchestrator.analyze；finalizer 在任务终结
        的 finally 链路里统一触发（覆盖 success / FAILED / timeout / cancel /
        sweep-stall 所有路径），用于把 pending 态的 chat_messages 落成终态。
        """
        if self._closed:
            raise RuntimeError("task registry has been shut down")

        entry = TaskEntry(
            trace_id=trace_id,
            user_id=request.user_id,
            session_id=request.session_id,
            request=request,
            assistant_message_id=assistant_message_id,
            user_message_id=user_message_id,
            finalizer=finalizer,
        )
        async with self._lock:
            self._entries[trace_id] = entry

        # 跨 worker 可见性种子：把 trace_runs 的占位行同步写入。
        # 前端收到 202 后如果立刻 GET /analyze/tasks/{id}/events 落到别的 worker，
        # Gate 1 preflight 回落 DB 时一定能查到 status=queued，不再误伤 404。
        # orchestrator.start_run 后续的 session.merge 会把 agent_name/status 刷成真实值。
        await asyncio.to_thread(self._seed_trace_run, entry)

        # 再发一条 queued 事件，前端订阅 SSE 时可通过 Last-Event-ID 重放
        self._publish_status(entry, TaskState.QUEUED)

        entry.task = asyncio.create_task(
            self._run(entry, runner_factory),
            name=f"analyze-async:{trace_id}",
        )
        _logger.info(
            "task queued: trace=%s user=%s session=%s concurrent=%d/%d",
            trace_id, entry.user_id, entry.session_id,
            self._max_concurrent_tasks - self._semaphore._value,  # noqa: SLF001
            self._max_concurrent_tasks,
        )
        return entry

    def _seed_trace_run(self, entry: TaskEntry) -> None:
        """POST 阶段同步写一行 trace_runs 占位，保证跨 worker 可见性。

        - 用 ``session.merge`` 而非 ``add``：trace_id 是主键，理论上唯一，
          但 merge 对"已存在"幂等（比如同进程 race 或 orchestrator 先启动的异常场景），
          也方便以后有重试语义时复用
        - ``agent_name="pending"`` 作为占位，orchestrator 的 TimingMiddleware.start_run
          之后会被 merge 覆盖成真实的 agent 名
        - 失败只 WARNING，不抛 —— seed 失败最坏是退化回原先的 race（跨 worker GET 可能 404），
          比让 POST 整体 500 要好
        """
        try:
            with self._session_factory() as session:
                session.merge(TraceRun(
                    trace_id=entry.trace_id,
                    agent_name="pending",
                    session_id=entry.session_id,
                    user_id=entry.user_id,
                    status="queued",
                    started_at=now_cn(),
                ))
                session.commit()
        except Exception:  # noqa: BLE001
            _logger.warning(
                "failed to seed trace_runs for %s; cross-worker GET /events "
                "may briefly 404 until orchestrator.start_run flushes",
                entry.trace_id,
                exc_info=True,
            )

    def get(self, trace_id: str) -> TaskEntry | None:
        return self._entries.get(trace_id)

    # ------------------------------------------------------------------
    # runner 内部实现
    # ------------------------------------------------------------------

    async def _run(
        self,
        entry: TaskEntry,
        runner_factory: RunnerFactory,
    ) -> None:
        # 注入 assistant_message_id 到 ContextVar，供 ReportAgent 流式输出时
        # 把 chunk 事件绑定到正确的 pending 气泡。trace_id 由 observability
        # 中间件维护，此处不重复。
        from core.tasks.context import current_assistant_message_id

        msg_id_token = current_assistant_message_id.set(
            str(entry.assistant_message_id)
            if entry.assistant_message_id is not None
            else None
        )
        try:
            async with self._semaphore:
                entry.started_at = now_cn()
                entry.state = TaskState.RUNNING
                self._publish_status(entry, TaskState.RUNNING)
                _logger.info(
                    "task running: trace=%s user=%s",
                    entry.trace_id, entry.user_id,
                )
                # 硬超时兜底：覆盖 orchestrator + 持久化 + chat 收尾的所有 I/O。
                # 正常路径 orchestrator 内部还有 response_timeout_seconds 精细超时；
                # 这里只是防止任何遗漏的无保护 I/O 把 entry.state 卡在 RUNNING。
                try:
                    result = await asyncio.wait_for(
                        runner_factory(entry),
                        timeout=self._runner_hard_timeout,
                    )
                except asyncio.TimeoutError:
                    self._stalled_count += 1
                    _logger.error(
                        "runner stalled beyond hard timeout %.1fs: "
                        "trace=%s (stalled_count=%d); forcing ERROR(RUNNER_STALLED). "
                        "trace_runs 可能已由 observability worker 写终态，"
                        "但 runner 尾部 I/O 卡住导致内存 entry 未转状态。",
                        self._runner_hard_timeout,
                        entry.trace_id,
                        self._stalled_count,
                    )
                    entry.state = TaskState.ERROR
                    entry.error = ErrorInfo(
                        code="RUNNER_STALLED",
                        message=(
                            f"runner 协程在 {self._runner_hard_timeout:.0f}s 内未返回，"
                            "已强制终结；详情请查 trace_runs 表。"
                        ),
                    )
                else:
                    entry.result = result
                    # 分析本身可能以"业务失败"结束（status=FAILED），
                    # 即使 runner 没抛异常也要把 entry.state 映射为 ERROR，
                    # 这样 SSE done / 快照 / chat_messages 三处状态保持一致。
                    if getattr(result, "status", None) == AnalysisStatus.FAILED:
                        entry.state = TaskState.ERROR
                        entry.error = getattr(result, "error", None) or ErrorInfo(
                            code="ANALYZE_FAILED",
                            message="分析失败（详情缺失）",
                        )
                    else:
                        entry.state = TaskState.OK
                        entry.error = None
        except asyncio.CancelledError:
            # Plan C 的 sweep 会在外部 cancel 前先把 entry 强制转到 ERROR(RUNNER_STALLED)。
            # 此时若再覆盖为 ABORTED 会丢失"被 sweep 纠偏"的归因信息。
            if entry.state not in TERMINAL_STATES:
                entry.state = TaskState.ABORTED
                entry.error = ErrorInfo(
                    code="ABORTED", message="任务被取消（通常由服务关闭触发）"
                )
            raise
        except Exception as exc:  # noqa: BLE001
            _logger.exception("async analyze task failed: trace=%s", entry.trace_id)
            entry.state = TaskState.ERROR
            entry.error = ErrorInfo(code="API_ERROR", message=str(exc))
        finally:
            entry.finished_at = now_cn()
            if entry.started_at is not None:
                entry.duration_ms = (
                    entry.finished_at - entry.started_at
                ).total_seconds() * 1000.0
            # 发 done 之前先等 trace_runs 的 run_end 被 commit 到 DB，
            # 避免前端收到 done 后立刻查快照落到其他 worker，
            # 后者回落 DB 时 trace_runs 仍是 running（docs/issue SSE 与快照不一致问题）。
            # 失败只降级 WARNING,仍照常 publish_done——不能因为 DB 抖动把 SSE 吊死。
            await self._await_trace_run_flushed(entry.trace_id)
            # 业务收尾（chat_messages 等）必须在 publish_done 之前执行:
            # 前端收到 SSE done 后会立刻拉 sessions 判断消息终态,这条顺序保证
            # done 到达时 chat_messages 已经落到 success / error(Issue 3 修复核心)。
            await self._run_finalizer(entry)
            self._publish_done(entry)
            self._bus.close(entry.trace_id)
            # 恢复 ContextVar，避免协程复用线程时污染后续任务。
            current_assistant_message_id.reset(msg_id_token)
            _logger.info(
                "task finished: trace=%s state=%s duration=%.1fms",
                entry.trace_id,
                entry.state.value if hasattr(entry.state, "value") else entry.state,
                entry.duration_ms or 0,
            )

    async def _await_trace_run_flushed(self, trace_id: str) -> None:
        """发 done 前阻塞等 trace_runs 的 run_end commit 到 DB。

        把 ``TraceStore.flush_now_sync``（同步、基于 threading.Event）放到
        ``asyncio.to_thread`` 里跑，避免阻塞事件循环。TraceStore 未初始化
        或不支持 barrier（测试场景）时直接跳过。
        """
        try:
            from core.observability.store import get_trace_store
        except Exception:  # noqa: BLE001
            return
        store = get_trace_store()
        if store is None or not hasattr(store, "flush_now_sync"):
            return
        try:
            ok = await asyncio.to_thread(
                store.flush_now_sync,
                trace_id,
                timeout=self._trace_flush_barrier_timeout,
            )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "trace-run flush barrier raised on trace %s; publishing done anyway",
                trace_id,
                exc_info=True,
            )
            return
        if not ok:
            _logger.warning(
                "trace-run flush barrier timed out on trace %s; "
                "snapshot may lag behind SSE done briefly",
                trace_id,
            )

    async def _run_finalizer(self, entry: TaskEntry) -> None:
        """在 publish_done 前执行业务收尾回调（如 chat_messages 终态写入）。

        - 幂等：通过 ``entry.finalized`` 标志防止 _run.finally 与 sweep 两条
          路径重复调度时真的跑两次（首先进入的 coroutine 抢到标志位，后到的
          短路返回）
        - 隔离：finalizer 自身异常只记 WARNING，不阻塞 publish_done；SSE 前
          端的终态可见性优先级高于 chat_messages 的一致性
        - 超时：5 秒硬超时，避免 DB 死锁把整个任务吊死
        """
        if entry.finalizer is None or entry.finalized:
            return
        entry.finalized = True
        try:
            await asyncio.wait_for(entry.finalizer(entry), timeout=5.0)
        except asyncio.TimeoutError:
            _logger.warning(
                "finalizer timed out on trace %s after 5.0s; "
                "chat_messages 可能仍停在 pending,将靠启动期 recover 或后续 sweep 收敛",
                entry.trace_id,
            )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "finalizer failed on trace %s; "
                "chat_messages 可能仍停在 pending",
                entry.trace_id,
                exc_info=True,
            )

    def _publish_status(self, entry: TaskEntry, state: TaskState) -> None:
        seq = self._bus.next_seq(entry.trace_id)
        self._bus.publish(
            entry.trace_id,
            {
                "type": "status",
                "trace_id": entry.trace_id,
                "ts": now_cn().isoformat(),
                "seq": seq,
                "replay_safe": True,
                "state": state.value,
            },
        )

    def _publish_done(self, entry: TaskEntry) -> None:
        seq = self._bus.next_seq(entry.trace_id)
        payload: dict[str, Any] = {
            "type": "done",
            "trace_id": entry.trace_id,
            "ts": now_cn().isoformat(),
            "seq": seq,
            "replay_safe": True,
            "status": entry.state.value,
            "duration_ms": entry.duration_ms,
        }
        if entry.result is not None:
            payload["anomaly_count"] = len(entry.result.anomalies)
        if entry.error is not None:
            payload["error"] = entry.error.model_dump()
        self._bus.publish(entry.trace_id, payload)

    def _force_stalled(self, entry: TaskEntry, age_seconds: float) -> None:
        """Plan C：sweep 发现僵尸 RUNNING entry 时的强制纠偏。

        - 直接把 entry 转成 ERROR(RUNNER_STALLED) + 补齐 finished_at / duration_ms
        - 发 done 让 SSE / poll 感知终态
        - close 事件总线，后续 publish 会被 RedisEventBus 的 is_closed 短路
        - cancel 协程释放资源；_run 的 except CancelledError 分支会检查
          ``entry.state in TERMINAL_STATES``，不会覆盖为 ABORTED

        调用时 entry.state 必然是 RUNNING（由 _sweep_once 筛选保证）。
        """
        self._stalled_count += 1
        threshold = self._runner_hard_timeout + self._runner_stall_grace
        _logger.error(
            "task registry sweep detected stalled RUNNING entry: trace=%s "
            "age=%.1fs (threshold=%.1fs, stalled_count=%d); "
            "forcing ERROR(RUNNER_STALLED). wait_for 兜底失灵或被屏蔽，"
            "可能是事件循环被长时间阻塞。",
            entry.trace_id,
            age_seconds,
            threshold,
            self._stalled_count,
        )
        entry.state = TaskState.ERROR
        entry.error = ErrorInfo(
            code="RUNNER_STALLED",
            message=(
                f"runner 协程运行 {age_seconds:.0f}s 未终结（阈值 {threshold:.0f}s，"
                "sweep 强制纠偏）；详情请查 trace_runs 表。"
            ),
        )
        entry.finished_at = now_cn()
        if entry.started_at is not None:
            entry.duration_ms = (
                entry.finished_at - entry.started_at
            ).total_seconds() * 1000.0
        # 僵尸纠偏路径也要触发 finalizer,否则 chat_messages 会永远停在 pending
        # (_run.finally 在 CancelledError 后虽然也会走 finalizer,但那时
        # entry.state 可能已被 _run 的 except 分支覆盖,此处先占位更可靠)。
        if entry.finalizer is not None and not entry.finalized:
            try:
                asyncio.create_task(
                    self._run_finalizer(entry),
                    name=f"stalled-finalize:{entry.trace_id}",
                )
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "failed to schedule finalizer during stall sweep: trace=%s",
                    entry.trace_id,
                    exc_info=True,
                )
        try:
            self._publish_done(entry)
        except Exception:  # noqa: BLE001
            _logger.warning(
                "publish_done failed during stall sweep: trace=%s",
                entry.trace_id,
                exc_info=True,
            )
        try:
            self._bus.close(entry.trace_id)
        except Exception:  # noqa: BLE001
            _logger.warning(
                "bus.close failed during stall sweep: trace=%s",
                entry.trace_id,
                exc_info=True,
            )
        if entry.task is not None and not entry.task.done():
            entry.task.cancel()

    def stalled_count(self) -> int:
        """累计被强制终结的僵尸 entry 数量（wait_for + sweep 两个路径共享计数）。

        供外部监控 / 告警读取；运行期只增不减，进程重启清零。
        """
        return self._stalled_count

    # ------------------------------------------------------------------
    # 后台清理
    # ------------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(self._sweep_interval)
            except asyncio.CancelledError:
                return
            try:
                self._sweep_once()
            except Exception:  # noqa: BLE001
                _logger.exception("task registry sweep failed")

    def _sweep_once(self) -> None:
        now = time.time()
        to_drop: list[str] = []
        to_force: list[tuple[TaskEntry, float]] = []
        stall_threshold = self._runner_hard_timeout + self._runner_stall_grace
        for trace_id, entry in list(self._entries.items()):
            # 僵尸 RUNNING 纠偏（Plan C）：_run 的 wait_for 兜底是第一道闸；
            # 极端场景下事件循环被阻塞、wait_for 本身失灵时，这里作为第二道闸
            # 强制把老化的 RUNNING entry 推到 ERROR(RUNNER_STALLED)。
            if entry.state == TaskState.RUNNING:
                if entry.started_at is not None:
                    age = now - entry.started_at.timestamp()
                    if age >= stall_threshold:
                        to_force.append((entry, age))
                continue
            if entry.state not in TERMINAL_STATES or entry.finished_at is None:
                continue
            age = now - entry.finished_at.timestamp()
            if age >= self._ttl:
                to_drop.append(trace_id)
        for entry, age in to_force:
            self._force_stalled(entry, age)
        for trace_id in to_drop:
            self._entries.pop(trace_id, None)
            self._bus.drop(trace_id)
        if to_drop:
            _logger.info("task registry swept %d expired entries", len(to_drop))
        # 孤儿 pending 消息兜底：finalizer + 启动 recover 都失效的极端场景
        # （进程 OOM/Kill 在 POST 之后、finalizer 未跑之前就挂掉且立刻重启
        # 也不会触发 _mark_stale_as_aborted；或 finalizer 连续 5s 超时）。
        try:
            affected = self._sweep_orphan_pending_chat_messages()
            if affected:
                _logger.warning(
                    "task registry sweep marked %d orphan pending chat_messages as error "
                    "(age>%ds); 这批消息对应的 finalizer 链路全部失败",
                    affected,
                    self._orphan_pending_chat_max_age,
                )
        except Exception:  # noqa: BLE001
            _logger.exception("orphan pending chat_messages sweep failed")

    # ------------------------------------------------------------------
    # 启动兜底：把跨进程残留状态收敛
    # ------------------------------------------------------------------

    def _sweep_orphan_pending_chat_messages(self) -> int:
        """把超龄且仍处于 pending 的 chat_messages 推到 error。

        该 sweep 是 finalizer 的最后一道兜底：
        1. registry._run_finalizer 是第一道闸（覆盖绝大多数终态路径）
        2. _mark_stale_as_aborted 是第二道闸（仅在启动/关闭触发）
        3. 本方法是第三道闸（进程存活期间的周期性清理）

        阈值由 ``orphan_pending_chat_max_age_sec`` 控制，默认 1800 秒。
        必须足够大以避开正常业务最坏耗时（runner_hard_timeout 600 + 余量），
        太小会误杀仍在正常运行的任务。
        """
        from datetime import timedelta

        threshold = now_cn() - timedelta(
            seconds=self._orphan_pending_chat_max_age
        )
        with self._session_factory() as session:
            affected = session.execute(
                update(chat_messages_table)
                .where(
                    chat_messages_table.c.status == "pending",
                    chat_messages_table.c.created_at < threshold,
                )
                .values(
                    status="error",
                    content="任务在后端无响应中终结,请重新发起",
                )
            ).rowcount or 0
            session.commit()
        return int(affected)

    def _mark_stale_as_aborted(self) -> dict[str, int]:
        """同步方法：跨进程启动/关闭时调用。"""
        affected_runs = 0
        affected_msgs = 0
        with self._session_factory() as session:
            affected_runs = session.execute(
                update(TraceRun)
                .where(TraceRun.status == "running")
                .values(
                    status="aborted",
                    finished_at=now_cn(),
                    error="process restarted or shutdown",
                )
            ).rowcount or 0
            affected_msgs = session.execute(
                update(chat_messages_table)
                .where(chat_messages_table.c.status == "pending")
                .values(
                    status="error",
                    content="任务因服务重启中断，请重新发起",
                )
            ).rowcount or 0
            session.commit()
        if affected_runs or affected_msgs:
            _logger.warning(
                "task registry recovery: marked %d trace_runs aborted, "
                "%d chat_messages error",
                affected_runs,
                affected_msgs,
            )
        return {"trace_runs": affected_runs, "chat_messages": affected_msgs}


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------


_registry: TaskRegistry | None = None


def init_task_registry(
    *,
    event_bus: EventBusProtocol,
    session_factory: sessionmaker[Session],
    max_concurrent_tasks: int = 5,
    result_cache_ttl_sec: int = 600,
    sweep_interval_sec: int = 60,
    trace_flush_barrier_timeout: float = 2.0,
    runner_hard_timeout_seconds: float = 600.0,
    runner_stall_grace_seconds: float = 60.0,
    orphan_pending_chat_max_age_sec: int = 1800,
) -> TaskRegistry:
    """初始化全局 TaskRegistry（幂等）。"""
    global _registry
    if _registry is None:
        _registry = TaskRegistry(
            event_bus=event_bus,
            session_factory=session_factory,
            max_concurrent_tasks=max_concurrent_tasks,
            result_cache_ttl_sec=result_cache_ttl_sec,
            sweep_interval_sec=sweep_interval_sec,
            trace_flush_barrier_timeout=trace_flush_barrier_timeout,
            runner_hard_timeout_seconds=runner_hard_timeout_seconds,
            runner_stall_grace_seconds=runner_stall_grace_seconds,
            orphan_pending_chat_max_age_sec=orphan_pending_chat_max_age_sec,
        )
    return _registry


def get_task_registry() -> TaskRegistry | None:
    return _registry


def shutdown_task_registry() -> None:
    global _registry
    _registry = None
