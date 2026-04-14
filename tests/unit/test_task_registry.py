"""TaskRegistry 单元测试。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from api.schemas.analysis import AnalysisRequest, AnalysisResult, AnalysisStatus, AnalysisType
from core.chat.tables import chat_messages_table, chat_sessions_table
from core.observability.tables import TraceRun
from core.tasks.events import EventBus
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
def bus() -> EventBus:
    return EventBus()


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
    seqs = [e["type"] for e in bus.buffered("t-ok")]
    assert "status" in seqs and "done" in seqs
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
