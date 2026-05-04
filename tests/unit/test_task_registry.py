"""TaskRegistry 单元测试。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from api.schemas.analysis import AnalysisRequest, AnalysisResult, AnalysisStatus, AnalysisType
from core.chat.tables import chat_messages_table, chat_sessions_table
from core.observability.tables import TraceRun
from core.tasks.registry import TaskEntry, TaskRegistry
from core.tasks.schemas import TaskState
from core.time_utils import now_cn


def _req(user_id: str = "u1", session_id: str = "s1") -> AnalysisRequest:
    return AnalysisRequest(query="测试异常", user_id=user_id, session_id=session_id)


def _ok_result(trace_id: str) -> AnalysisResult:
    return AnalysisResult(
        report_id=trace_id,
        trace_id=trace_id,
        status=AnalysisStatus.SUCCESS,
        analysis_type=AnalysisType.COMPREHENSIVE,
        query="q",
        user_id="u1",
        session_id="s1",
        time_range="30d",
    )


@pytest.fixture()
def bus():
    fakeredis = pytest.importorskip("fakeredis")
    from fakeredis import aioredis as fake_aioredis

    from core.tasks.events_redis import RedisEventBus

    server = fakeredis.FakeServer()
    b = RedisEventBus(redis_url="redis://fake", key_prefix="t:reg")
    b._sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    b._async = fake_aioredis.FakeRedis(server=server, decode_responses=True)
    return b


@pytest.fixture()
def registry(bus, db_session_factory) -> TaskRegistry:
    return TaskRegistry(
        event_bus=bus,
        session_factory=db_session_factory,
        max_concurrent_tasks=2,
        result_cache_ttl_sec=1,
        sweep_interval_sec=60,
    )


@pytest.mark.asyncio
async def test_submit_runs_runner_and_reaches_ok(registry, bus) -> None:
    entry_ref: list[TaskEntry] = []

    async def runner(entry: TaskEntry) -> AnalysisResult:
        entry_ref.append(entry)
        return _ok_result(entry.trace_id)

    entry = await registry.submit(
        _req(), trace_id="t-ok",
        assistant_message_id=None, user_message_id=None,
        runner_factory=runner,
    )
    await entry.task  # type: ignore[arg-type]

    assert entry.state == TaskState.OK
    assert entry.result is not None and entry.result.trace_id == "t-ok"
    assert entry.duration_ms is not None
    assert entry_ref and entry_ref[0] is entry

    # 事件总线应当收到 queued / running / done，SSE 订阅可重放
    buffered = bus.buffered("t-ok")
    seqs = [e["type"] for e in buffered]
    assert "status" in seqs and "done" in seqs
    assert all(e["replay_safe"] is True for e in buffered)
    assert bus.is_closed("t-ok")


@pytest.mark.asyncio
async def test_failed_result_maps_to_error_state(registry, bus) -> None:
    """runner 正常返回但 result.status=FAILED 时 entry.state 应为 ERROR。"""
    from api.schemas.analysis import ErrorInfo

    async def runner(entry: TaskEntry) -> AnalysisResult:
        return AnalysisResult(
            report_id=entry.trace_id,
            trace_id=entry.trace_id,
            status=AnalysisStatus.FAILED,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query="q",
            user_id="u1",
            session_id="s1",
            time_range="",
            error=ErrorInfo(code="LLM_CONNECTION_ERROR", message="conn reset"),
        )

    entry = await registry.submit(
        _req(), trace_id="t-failed-result",
        assistant_message_id=None, user_message_id=None,
        runner_factory=runner,
    )
    await entry.task  # type: ignore[arg-type]

    assert entry.state == TaskState.ERROR
    assert entry.error is not None
    assert entry.error.code == "LLM_CONNECTION_ERROR"

    done = [e for e in bus.buffered("t-failed-result") if e.get("type") == "done"]
    assert done and done[0]["status"] == "error"


@pytest.mark.asyncio
async def test_submit_runner_exception_marks_error(registry) -> None:
    async def runner(_entry: TaskEntry) -> AnalysisResult:
        raise RuntimeError("boom")

    entry = await registry.submit(
        _req(), trace_id="t-err",
        assistant_message_id=None, user_message_id=None,
        runner_factory=runner,
    )
    await asyncio.gather(entry.task, return_exceptions=True)  # type: ignore[arg-type]

    assert entry.state == TaskState.ERROR
    assert entry.error is not None
    assert "boom" in entry.error.message


@pytest.mark.asyncio
async def test_cancel_marks_aborted(registry) -> None:
    started = asyncio.Event()

    async def runner(_entry: TaskEntry) -> AnalysisResult:
        started.set()
        await asyncio.sleep(10)
        raise AssertionError("should not reach")

    entry = await registry.submit(
        _req(), trace_id="t-cancel",
        assistant_message_id=None, user_message_id=None,
        runner_factory=runner,
    )
    await started.wait()
    assert entry.task is not None
    entry.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entry.task
    assert entry.state == TaskState.ABORTED
    assert entry.error is not None and entry.error.code == "ABORTED"


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency(registry) -> None:
    active = 0
    peak = 0
    done_event = asyncio.Event()

    async def runner(_entry: TaskEntry) -> AnalysisResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.1)
        active -= 1
        return _ok_result(_entry.trace_id)

    tasks: list[asyncio.Task] = []
    for i in range(5):
        e = await registry.submit(
            _req(session_id=f"s{i}"), trace_id=f"t-{i}",
            assistant_message_id=None, user_message_id=None,
            runner_factory=runner,
        )
        tasks.append(e.task)  # type: ignore[arg-type]

    done_event.set()
    await asyncio.gather(*tasks)
    # max_concurrent_tasks=2
    assert peak <= 2


@pytest.mark.asyncio
async def test_get_returns_entry(registry) -> None:
    async def runner(entry: TaskEntry) -> AnalysisResult:
        return _ok_result(entry.trace_id)

    entry = await registry.submit(
        _req(), trace_id="t-get",
        assistant_message_id=None, user_message_id=None,
        runner_factory=runner,
    )
    await entry.task  # type: ignore[arg-type]
    assert registry.get("t-get") is entry
    assert registry.get("not-exists") is None


@pytest.mark.asyncio
async def test_sweep_once_evicts_expired(registry, bus) -> None:
    async def runner(entry: TaskEntry) -> AnalysisResult:
        return _ok_result(entry.trace_id)

    entry = await registry.submit(
        _req(), trace_id="t-sweep",
        assistant_message_id=None, user_message_id=None,
        runner_factory=runner,
    )
    await entry.task  # type: ignore[arg-type]
    # 人为将 finished_at 往前推超过 TTL
    entry.finished_at = now_cn() - timedelta(seconds=10)
    registry._sweep_once()
    assert registry.get("t-sweep") is None
    assert bus.buffered("t-sweep") == []


@pytest.mark.asyncio
async def test_recover_on_startup_marks_stale(registry, db_session_factory) -> None:
    # 构造一行 running 的 trace_runs + 一个带 pending 消息的会话
    with db_session_factory() as s:
        s.add(TraceRun(
            trace_id="stale-trace",
            agent_name="p2p",
            session_id="sess-x",
            user_id="u1",
            status="running",
            started_at=now_cn(),
        ))
        s.execute(chat_sessions_table.insert().values(
            id="sess-x", user_id="u1", title="x",
            title_auto=True, message_count=1,
        ))
        s.execute(chat_messages_table.insert().values(
            id="m-pending", session_id="sess-x", role="assistant",
            content="", status="pending", trace_id="stale-trace",
        ))
        s.commit()

    counts = registry.recover_on_startup()
    assert counts["trace_runs"] >= 1
    assert counts["chat_messages"] >= 1

    with db_session_factory() as s:
        run = s.get(TraceRun, "stale-trace")
        assert run is not None and run.status == "aborted"
        msg = s.execute(
            select(chat_messages_table).where(chat_messages_table.c.id == "m-pending")
        ).fetchone()
        assert msg is not None and msg.status == "error"


@pytest.mark.asyncio
async def test_shutdown_cancels_running_tasks(registry, db_session_factory) -> None:
    started = asyncio.Event()

    async def runner(_entry: TaskEntry) -> AnalysisResult:
        started.set()
        await asyncio.sleep(10)
        raise AssertionError

    entry = await registry.submit(
        _req(), trace_id="t-shut",
        assistant_message_id=None, user_message_id=None,
        runner_factory=runner,
    )
    await started.wait()
    await registry.shutdown(timeout=2.0)
    assert entry.state == TaskState.ABORTED


# ---------------------------------------------------------------------------
# Plan A：runner 硬超时兜底（wait_for）——防止 entry.state 卡 RUNNING
# 背景：registry._run 内的 runner_factory 协程里存在无 wait_for 保护的 DB I/O
# （如 orchestrator._persist_report），生产上观察到 poll 一直返回 running。
# ---------------------------------------------------------------------------


@pytest.fixture()
def short_timeout_registry(bus, db_session_factory) -> TaskRegistry:
    """硬超时 0.2s / 宽限 0.1s 的 registry，用于超时类测试快速验证。"""
    return TaskRegistry(
        event_bus=bus,
        session_factory=db_session_factory,
        max_concurrent_tasks=2,
        result_cache_ttl_sec=60,
        sweep_interval_sec=60,
        runner_hard_timeout_seconds=0.2,
        runner_stall_grace_seconds=0.1,
    )


@pytest.mark.asyncio
async def test_runner_hard_timeout_forces_error_state(short_timeout_registry, bus) -> None:
    """runner 卡超过 runner_hard_timeout_seconds 时 entry 必须转 ERROR(RUNNER_STALLED)。"""

    async def stuck_runner(_entry: TaskEntry) -> AnalysisResult:
        # 故意睡远超硬超时的时间；wait_for 应该先于 sleep 完成前把我们 cancel
        await asyncio.sleep(5.0)
        raise AssertionError("runner should have been cancelled by wait_for")

    entry = await short_timeout_registry.submit(
        _req(), trace_id="t-stalled-wait-for",
        assistant_message_id=None, user_message_id=None,
        runner_factory=stuck_runner,
    )
    await asyncio.gather(entry.task, return_exceptions=True)  # type: ignore[arg-type]

    assert entry.state == TaskState.ERROR
    assert entry.error is not None
    assert entry.error.code == "RUNNER_STALLED"
    assert entry.finished_at is not None
    assert entry.duration_ms is not None
    # done 事件应带 RUNNER_STALLED 错误，前端与 SSE 订阅都能感知终态
    dones = [e for e in bus.buffered("t-stalled-wait-for") if e.get("type") == "done"]
    assert dones and dones[0]["status"] == "error"
    assert dones[0]["error"]["code"] == "RUNNER_STALLED"
    assert short_timeout_registry.stalled_count() == 1


@pytest.mark.asyncio
async def test_runner_under_hard_timeout_still_ok(short_timeout_registry) -> None:
    """runner 在硬超时内正常返回时，行为不应被破坏。"""

    async def fast_runner(entry: TaskEntry) -> AnalysisResult:
        await asyncio.sleep(0.02)  # 远低于 0.2s 硬超时
        return _ok_result(entry.trace_id)

    entry = await short_timeout_registry.submit(
        _req(), trace_id="t-fast",
        assistant_message_id=None, user_message_id=None,
        runner_factory=fast_runner,
    )
    await entry.task  # type: ignore[arg-type]

    assert entry.state == TaskState.OK
    assert entry.error is None
    assert short_timeout_registry.stalled_count() == 0


# ---------------------------------------------------------------------------
# Plan C：sweep 扫僵尸 RUNNING entry——wait_for 兜底失灵时的纵深防御
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_forces_stalled_running_entry(short_timeout_registry, bus) -> None:
    """模拟 wait_for 失灵：人为保持 entry.state=RUNNING 且 started_at 已经超龄，
    sweep 必须把它强制转到 ERROR(RUNNER_STALLED) 并发出 done + close bus。"""
    # 构造一个活跃的 RUNNING entry（runner 会被我们显式 cancel）
    started = asyncio.Event()

    async def hang_runner(_entry: TaskEntry) -> AnalysisResult:
        started.set()
        await asyncio.sleep(30)
        raise AssertionError

    entry = await short_timeout_registry.submit(
        _req(), trace_id="t-zombie",
        assistant_message_id=None, user_message_id=None,
        runner_factory=hang_runner,
    )
    await started.wait()
    # 短超时 fixture 下 wait_for 本会提前生效；
    # 这里把 started_at 人工往前拉，确保 sweep 判断为"超龄"时走 Plan C 路径。
    entry.started_at = now_cn() - timedelta(seconds=10)  # 远超 0.2+0.1
    # 先切掉 wait_for 那条路径：把 state 重置回 RUNNING（模拟 wait_for 失灵）
    entry.state = TaskState.RUNNING
    entry.error = None
    entry.finished_at = None

    short_timeout_registry._sweep_once()

    # sweep 应把 entry 转到 ERROR(RUNNER_STALLED) 并计数+1
    assert entry.state == TaskState.ERROR
    assert entry.error is not None and entry.error.code == "RUNNER_STALLED"
    assert entry.finished_at is not None
    assert short_timeout_registry.stalled_count() >= 1
    # done 事件被发出
    dones = [e for e in bus.buffered("t-zombie") if e.get("type") == "done"]
    assert dones and dones[-1]["status"] == "error"
    assert dones[-1]["error"]["code"] == "RUNNER_STALLED"
    # bus 被关闭，新订阅只会拿到 replay
    assert bus.is_closed("t-zombie")
    # 协程被 cancel；等它收尾
    await asyncio.gather(entry.task, return_exceptions=True)  # type: ignore[arg-type]
    # 外部 cancel 不应覆盖 Plan C 已设的 ERROR → 仍然是 ERROR(RUNNER_STALLED)
    assert entry.state == TaskState.ERROR
    assert entry.error is not None and entry.error.code == "RUNNER_STALLED"


@pytest.mark.asyncio
async def test_sweep_skips_running_within_threshold(short_timeout_registry) -> None:
    """未超龄的 RUNNING entry 不能被误杀。"""
    started = asyncio.Event()
    done_event = asyncio.Event()

    async def slow_but_within_budget(_entry: TaskEntry) -> AnalysisResult:
        started.set()
        await done_event.wait()
        return _ok_result(_entry.trace_id)

    entry = await short_timeout_registry.submit(
        _req(), trace_id="t-within",
        assistant_message_id=None, user_message_id=None,
        runner_factory=slow_but_within_budget,
    )
    await started.wait()
    # 人为把 started_at 推早 0.05s（小于 threshold=0.3s）模拟"跑了一会儿但还没超"
    entry.started_at = now_cn() - timedelta(seconds=0.05)
    entry.state = TaskState.RUNNING

    short_timeout_registry._sweep_once()

    # 不被 sweep 纠偏；stalled_count 不变
    assert entry.state == TaskState.RUNNING
    assert short_timeout_registry.stalled_count() == 0

    # 放行 runner 让 entry.task 正常收尾，避免影响其它用例
    done_event.set()
    await asyncio.gather(entry.task, return_exceptions=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Finalizer 收尾：所有终止路径（成功/业务失败/运行时异常/硬超时/cancel/stall-sweep）
# 都必须触发一次 finalizer，否则依赖它落地的 chat_messages.status 会停在 pending。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalizer_called_on_success(registry) -> None:
    calls: list[TaskState] = []

    async def finalizer(entry: TaskEntry) -> None:
        calls.append(entry.state)

    async def runner(entry: TaskEntry) -> AnalysisResult:
        return _ok_result(entry.trace_id)

    entry = await registry.submit(
        _req(), trace_id="t-fin-ok",
        assistant_message_id="m1", user_message_id=None,
        runner_factory=runner, finalizer=finalizer,
    )
    await entry.task  # type: ignore[arg-type]
    assert calls == [TaskState.OK]
    assert entry.finalized is True


@pytest.mark.asyncio
async def test_finalizer_called_on_business_failure(registry) -> None:
    from api.schemas.analysis import ErrorInfo

    calls: list[tuple[TaskState, str | None]] = []

    async def finalizer(entry: TaskEntry) -> None:
        err_code = entry.error.code if entry.error else None
        calls.append((entry.state, err_code))

    async def runner(entry: TaskEntry) -> AnalysisResult:
        return AnalysisResult(
            report_id=entry.trace_id,
            trace_id=entry.trace_id,
            status=AnalysisStatus.FAILED,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query="q", user_id="u1", session_id="s1", time_range="",
            error=ErrorInfo(code="LLM_CONNECTION_ERROR", message="boom"),
        )

    entry = await registry.submit(
        _req(), trace_id="t-fin-failed",
        assistant_message_id="m2", user_message_id=None,
        runner_factory=runner, finalizer=finalizer,
    )
    await entry.task  # type: ignore[arg-type]
    assert calls == [(TaskState.ERROR, "LLM_CONNECTION_ERROR")]


@pytest.mark.asyncio
async def test_finalizer_called_on_runner_exception(registry) -> None:
    """orchestrator 抛异常时,runner 里的老代码收不到 result,旧实现会让
    chat_messages 停在 pending;finalizer 放到 registry.finally 后必须被调。"""
    calls: list[TaskState] = []

    async def finalizer(entry: TaskEntry) -> None:
        calls.append(entry.state)

    async def runner(_entry: TaskEntry) -> AnalysisResult:
        raise RuntimeError("boom from orchestrator")

    entry = await registry.submit(
        _req(), trace_id="t-fin-raise",
        assistant_message_id="m3", user_message_id=None,
        runner_factory=runner, finalizer=finalizer,
    )
    await asyncio.gather(entry.task, return_exceptions=True)  # type: ignore[arg-type]
    assert calls == [TaskState.ERROR]
    assert entry.error is not None and "boom" in entry.error.message


@pytest.mark.asyncio
async def test_finalizer_called_on_cancel(registry) -> None:
    started = asyncio.Event()
    calls: list[TaskState] = []

    async def finalizer(entry: TaskEntry) -> None:
        calls.append(entry.state)

    async def runner(_entry: TaskEntry) -> AnalysisResult:
        started.set()
        await asyncio.sleep(10)
        raise AssertionError

    entry = await registry.submit(
        _req(), trace_id="t-fin-cancel",
        assistant_message_id="m4", user_message_id=None,
        runner_factory=runner, finalizer=finalizer,
    )
    await started.wait()
    assert entry.task is not None
    entry.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entry.task
    assert calls == [TaskState.ABORTED]


@pytest.mark.asyncio
async def test_finalizer_called_on_runner_hard_timeout(
    short_timeout_registry,
) -> None:
    calls: list[tuple[TaskState, str | None]] = []

    async def finalizer(entry: TaskEntry) -> None:
        err_code = entry.error.code if entry.error else None
        calls.append((entry.state, err_code))

    async def stuck(_entry: TaskEntry) -> AnalysisResult:
        await asyncio.sleep(5.0)
        raise AssertionError

    entry = await short_timeout_registry.submit(
        _req(), trace_id="t-fin-timeout",
        assistant_message_id="m5", user_message_id=None,
        runner_factory=stuck, finalizer=finalizer,
    )
    await asyncio.gather(entry.task, return_exceptions=True)  # type: ignore[arg-type]
    assert calls == [(TaskState.ERROR, "RUNNER_STALLED")]


@pytest.mark.asyncio
async def test_finalizer_called_on_stall_sweep(short_timeout_registry) -> None:
    """sweep 强制纠偏僵尸 RUNNING entry 时也必须触发 finalizer,
    否则 chat_messages 永远停在 pending(僵尸任务是 P0 场景)。"""
    started = asyncio.Event()
    calls: list[TaskState] = []

    async def finalizer(entry: TaskEntry) -> None:
        calls.append(entry.state)

    async def hang(_entry: TaskEntry) -> AnalysisResult:
        started.set()
        await asyncio.sleep(30)
        raise AssertionError

    entry = await short_timeout_registry.submit(
        _req(), trace_id="t-fin-sweep",
        assistant_message_id="m6", user_message_id=None,
        runner_factory=hang, finalizer=finalizer,
    )
    await started.wait()
    # 模拟 wait_for 失灵:强行把 entry 拉回 RUNNING + 超龄
    entry.started_at = now_cn() - timedelta(seconds=10)
    entry.state = TaskState.RUNNING
    entry.error = None
    entry.finished_at = None

    short_timeout_registry._sweep_once()

    # sweep 通过 asyncio.create_task 调度 finalizer,给调度器一轮机会
    for _ in range(10):
        if calls:
            break
        await asyncio.sleep(0.01)
    assert calls == [TaskState.ERROR]

    # 等 entry.task 自然收尾,防止影响其它用例
    await asyncio.gather(entry.task, return_exceptions=True)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_finalizer_exception_does_not_block_done(registry, bus) -> None:
    """finalizer 自身失败只打 WARNING,不能阻塞 publish_done—SSE 可见性优先。"""

    async def boom_finalizer(_entry: TaskEntry) -> None:
        raise RuntimeError("finalizer boom")

    async def runner(entry: TaskEntry) -> AnalysisResult:
        return _ok_result(entry.trace_id)

    entry = await registry.submit(
        _req(), trace_id="t-fin-boom",
        assistant_message_id="m7", user_message_id=None,
        runner_factory=runner, finalizer=boom_finalizer,
    )
    await entry.task  # type: ignore[arg-type]

    # done 仍然被发出;前端不受 finalizer 失败影响
    dones = [e for e in bus.buffered("t-fin-boom") if e.get("type") == "done"]
    assert dones and dones[-1]["status"] == "ok"
    # finalized 标志仍被置位,避免后续重试无限循环
    assert entry.finalized is True


@pytest.mark.asyncio
async def test_finalizer_timeout_degrades_gracefully(registry, bus) -> None:
    """finalizer 卡超过 5s 硬超时时降级 WARNING,不卡 done。"""

    async def slow_finalizer(_entry: TaskEntry) -> None:
        await asyncio.sleep(30)

    async def runner(entry: TaskEntry) -> AnalysisResult:
        return _ok_result(entry.trace_id)

    entry = await registry.submit(
        _req(), trace_id="t-fin-slow",
        assistant_message_id="m8", user_message_id=None,
        runner_factory=runner, finalizer=slow_finalizer,
    )
    # 把 registry 的 finalizer 超时从 5s 改到 0.1s 以加速测试
    original = registry._run_finalizer

    async def _patched(entry: TaskEntry) -> None:
        if entry.finalizer is None or entry.finalized:
            return
        entry.finalized = True
        try:
            await asyncio.wait_for(entry.finalizer(entry), timeout=0.1)
        except asyncio.TimeoutError:
            pass

    registry._run_finalizer = _patched  # type: ignore[method-assign]
    try:
        await asyncio.wait_for(entry.task, timeout=2.0)  # type: ignore[arg-type]
    finally:
        registry._run_finalizer = original  # type: ignore[method-assign]

    dones = [e for e in bus.buffered("t-fin-slow") if e.get("type") == "done"]
    assert dones and dones[-1]["status"] == "ok"


@pytest.mark.asyncio
async def test_sweep_orphan_pending_chat_messages(bus, db_session_factory) -> None:
    """模拟 finalizer 全链路失败留下的超龄 pending 消息,sweep 必须把它们推到 error。"""
    # 用极短阈值的 registry,方便测试
    short_orphan_registry = TaskRegistry(
        event_bus=bus,
        session_factory=db_session_factory,
        max_concurrent_tasks=2,
        result_cache_ttl_sec=60,
        sweep_interval_sec=60,
        runner_hard_timeout_seconds=0.2,
        runner_stall_grace_seconds=0.1,
        orphan_pending_chat_max_age_sec=1,  # 1 秒,几乎立即过期（>= 0.2+0.1）
    )

    # 建一条 session 及两条消息:一条超龄 pending、一条新 pending
    with db_session_factory() as s:
        s.execute(chat_sessions_table.insert().values(
            id="sess-orphan", user_id="u1", title="x",
            title_auto=True, message_count=2,
        ))
        # 超龄 pending:created_at 往前推 10 秒
        s.execute(chat_messages_table.insert().values(
            id="m-old-pending", session_id="sess-orphan", role="assistant",
            content="", status="pending", trace_id="trace-old",
            created_at=now_cn() - timedelta(seconds=10),
        ))
        # 新 pending:不该被扫到
        s.execute(chat_messages_table.insert().values(
            id="m-new-pending", session_id="sess-orphan", role="assistant",
            content="", status="pending", trace_id="trace-new",
            created_at=now_cn(),
        ))
        s.commit()

    short_orphan_registry._sweep_once()

    with db_session_factory() as s:
        old = s.execute(
            select(chat_messages_table).where(chat_messages_table.c.id == "m-old-pending")
        ).fetchone()
        new = s.execute(
            select(chat_messages_table).where(chat_messages_table.c.id == "m-new-pending")
        ).fetchone()
        assert old is not None and old.status == "error"
        assert "重新发起" in old.content
        assert new is not None and new.status == "pending"


# ---------------------------------------------------------------------------
# TTL 约束校验：orphan_pending_chat_max_age_sec 必须 >= stall_ceiling
# ---------------------------------------------------------------------------


def test_ttl_constraint_auto_corrects_when_orphan_age_too_small(
    bus, db_session_factory,
) -> None:
    """orphan_pending_chat_max_age_sec < runner_hard_timeout + stall_grace 时
    自动修正为 stall_ceiling * 2，防止误杀正在运行的任务的 chat_messages。"""
    reg = TaskRegistry(
        event_bus=bus,
        session_factory=db_session_factory,
        max_concurrent_tasks=2,
        result_cache_ttl_sec=60,
        sweep_interval_sec=60,
        runner_hard_timeout_seconds=600.0,
        runner_stall_grace_seconds=60.0,
        orphan_pending_chat_max_age_sec=500,  # < 600+60=660
    )
    assert reg._orphan_pending_chat_max_age == 1320  # (600+60)*2


def test_ttl_constraint_keeps_valid_value(bus, db_session_factory) -> None:
    """orphan_pending_chat_max_age_sec >= stall_ceiling 时保持原值。"""
    reg = TaskRegistry(
        event_bus=bus,
        session_factory=db_session_factory,
        max_concurrent_tasks=2,
        result_cache_ttl_sec=60,
        sweep_interval_sec=60,
        runner_hard_timeout_seconds=100.0,
        runner_stall_grace_seconds=20.0,
        orphan_pending_chat_max_age_sec=200,  # >= 100+20=120
    )
    assert reg._orphan_pending_chat_max_age == 200


def test_ttl_constraint_boundary_equal_is_valid(bus, db_session_factory) -> None:
    """orphan_pending_chat_max_age_sec == stall_ceiling 时不触发修正。"""
    reg = TaskRegistry(
        event_bus=bus,
        session_factory=db_session_factory,
        max_concurrent_tasks=2,
        result_cache_ttl_sec=60,
        sweep_interval_sec=60,
        runner_hard_timeout_seconds=50.0,
        runner_stall_grace_seconds=10.0,
        orphan_pending_chat_max_age_sec=60,  # == 50+10
    )
    assert reg._orphan_pending_chat_max_age == 60
