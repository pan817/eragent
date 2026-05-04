"""analyze_async 路由单元测试。

所有测试共享同一个 event loop（pytest-asyncio），用 ``httpx.AsyncClient + ASGITransport``
直接驱动 FastAPI 应用，避免 TestClient 每请求新建 loop 导致后台 task 被回收。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import api.routes.analyze_async as async_module
from api.routes.analyze_async import router as async_router
from api.schemas.analysis import (
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
)
from core.chat import ChatRepository, init_chat_repository
from core.observability.store import init_trace_store, shutdown_trace_store
from core.tasks.events import init_event_bus, shutdown_event_bus
from core.tasks.registry import init_task_registry, shutdown_task_registry


class _FakeOrchestrator:
    def __init__(self, *, should_fail: bool = False, delay: float = 0.01) -> None:
        self.should_fail = should_fail
        self.delay = delay
        self.calls: list[str] = []

    async def analyze(self, request, *, trace_id: str | None = None):  # type: ignore[no-untyped-def]
        self.calls.append(trace_id or "")
        await asyncio.sleep(self.delay)
        if self.should_fail:
            return AnalysisResult(
                report_id="r",
                trace_id=trace_id or "",
                status=AnalysisStatus.FAILED,
                analysis_type=AnalysisType.COMPREHENSIVE,
                query=request.query,
                user_id=request.user_id,
                session_id=request.session_id,
                time_range="",
                report_markdown="",
            )
        return AnalysisResult(
            report_id="r",
            trace_id=trace_id or "",
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query=request.query,
            user_id=request.user_id,
            session_id=request.session_id,
            time_range="30d",
            report_markdown="# 报告\n正常结果",
            duration_ms=10.0,
        )


@pytest_asyncio.fixture()
async def app_env(db_session_factory, monkeypatch):
    fake = _FakeOrchestrator()
    monkeypatch.setattr(
        async_module, "_get_orchestrator", lambda: fake, raising=True
    )
    init_trace_store(db_session_factory)
    bus = init_event_bus()
    registry = init_task_registry(
        event_bus=bus,
        session_factory=db_session_factory,
        max_concurrent_tasks=4,
        result_cache_ttl_sec=60,
        # FakeOrchestrator 不走 TimingMiddleware，run_end 不会入队；
        # 让 barrier 快速超时后 WARNING 放行，避免把每个测试都阻塞 2s。
        trace_flush_barrier_timeout=0.1,
    )
    chat_repo = ChatRepository(db_session_factory)
    init_chat_repository(chat_repo)

    fastapi_app = FastAPI()
    fastapi_app.include_router(async_router)

    yield fastapi_app, fake, chat_repo, registry

    await registry.shutdown(timeout=1.0)
    shutdown_task_registry()
    shutdown_event_bus()
    shutdown_trace_store()
    init_chat_repository(None)  # type: ignore[arg-type]


def _make_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _post_submit(
    client: AsyncClient, *, query: str = "测试", session_id: str = ""
) -> dict[str, Any]:
    resp = await client.post(
        "/analyze/async",
        json={
            "query": query,
            "user_id": "u1",
            "session_id": session_id,
            "auto_persist": True,
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_submit_returns_ack_and_persists_pending_message(app_env) -> None:
    fastapi_app, _fake, chat_repo, _registry = app_env
    async with _make_client(fastapi_app) as client:
        ack = await _post_submit(client)
        assert ack["status"] == "queued"
        assert ack["trace_id"]
        assert ack["assistant_message_id"]
        assert ack["user_message_id"]
        assert ack["poll_url"].endswith(ack["trace_id"])

        session = await asyncio.to_thread(
            chat_repo.get_session_with_messages,
            "u1", ack["session_id"], 10,
        )
        assert session is not None
        msgs = session["messages"]
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user" and msgs[0]["status"] == "success"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["trace_id"] == ack["trace_id"]


@pytest.mark.asyncio
async def test_end_to_end_success_updates_assistant_message(app_env) -> None:
    fastapi_app, fake, chat_repo, registry = app_env
    async with _make_client(fastapi_app) as client:
        ack = await _post_submit(client, query="分析三路匹配")
        entry = registry.get(ack["trace_id"])
        assert entry is not None and entry.task is not None
        await asyncio.wait_for(entry.task, timeout=2.0)

        session = await asyncio.to_thread(
            chat_repo.get_session_with_messages,
            "u1", ack["session_id"], 10,
        )
        asst = session["messages"][1]
        assert asst["status"] == "success"
        assert "正常结果" in asst["content"]

        snap = (await client.get(ack["poll_url"])).json()
        assert snap["status"] == "ok"
        assert snap["result"]["report_markdown"].startswith("# 报告")
        assert fake.calls == [ack["trace_id"]]


@pytest.mark.asyncio
async def test_failed_status_propagates_to_snapshot_and_sse(
    app_env, monkeypatch
) -> None:
    """orchestrator 返回 status=FAILED 时，snapshot / chat / SSE done 均为 error。"""
    fastapi_app, _fake, chat_repo, registry = app_env

    class _FailingOrchestrator:
        async def analyze(self, req, *, trace_id: str | None = None):  # type: ignore[no-untyped-def]
            from api.schemas.analysis import ErrorInfo

            return AnalysisResult(
                report_id="r",
                trace_id=trace_id or "",
                status=AnalysisStatus.FAILED,
                analysis_type=AnalysisType.COMPREHENSIVE,
                query=req.query,
                user_id=req.user_id,
                session_id=req.session_id,
                time_range="",
                report_markdown="",
                error=ErrorInfo(
                    code="LLM_CONNECTION_ERROR",
                    message="dashscope 无法访问",
                ),
            )

    monkeypatch.setattr(
        async_module, "_get_orchestrator", lambda: _FailingOrchestrator(),
        raising=True,
    )
    async with _make_client(fastapi_app) as client:
        ack = await _post_submit(client)
        entry = registry.get(ack["trace_id"])
        assert entry is not None and entry.task is not None
        await asyncio.wait_for(entry.task, timeout=2.0)

        # snapshot: status=error + error 字段非空
        snap = (await client.get(ack["poll_url"])).json()
        assert snap["status"] == "error"
        assert snap["error"]["code"] == "LLM_CONNECTION_ERROR"
        assert "dashscope" in snap["error"]["message"]

        # chat: assistant 消息 status=error
        session = await asyncio.to_thread(
            chat_repo.get_session_with_messages,
            "u1", ack["session_id"], 10,
        )
        asst = session["messages"][1]
        assert asst["status"] == "error"
        assert "dashscope" in asst["content"]

        # SSE done.status=error
        types_seen: list[str] = []
        async with client.stream("GET", ack["stream_url"]) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[len("data: "):])
                    types_seen.append(ev.get("type"))
                    if ev.get("type") == "done":
                        assert ev["status"] == "error"
                        assert ev["error"]["code"] == "LLM_CONNECTION_ERROR"
                        break
        assert "done" in types_seen


@pytest.mark.asyncio
async def test_failure_marks_assistant_error(
    app_env, monkeypatch
) -> None:
    fastapi_app, _fake, chat_repo, registry = app_env
    failing = _FakeOrchestrator(should_fail=True)
    monkeypatch.setattr(
        async_module, "_get_orchestrator", lambda: failing, raising=True
    )
    async with _make_client(fastapi_app) as client:
        ack = await _post_submit(client)
        entry = registry.get(ack["trace_id"])
        assert entry is not None and entry.task is not None
        await asyncio.wait_for(entry.task, timeout=2.0)
        session = await asyncio.to_thread(
            chat_repo.get_session_with_messages,
            "u1", ack["session_id"], 10,
        )
        asst = session["messages"][1]
        assert asst["status"] == "error"


@pytest.mark.asyncio
async def test_snapshot_404_for_unknown_trace(app_env) -> None:
    fastapi_app, *_ = app_env
    async with _make_client(fastapi_app) as client:
        resp = await client.get("/analyze/tasks/not-exist")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_regenerate_of_updates_existing_assistant(app_env) -> None:
    fastapi_app, _fake, chat_repo, registry = app_env
    async with _make_client(fastapi_app) as client:
        # 先跑一次，拿到 assistant_message_id
        first = await _post_submit(client, query="第一次")
        entry = registry.get(first["trace_id"])
        await asyncio.wait_for(entry.task, timeout=2.0)  # type: ignore[arg-type]

        # 用 regenerate_of 请求复用同一个 assistant 消息
        resp = await client.post(
            "/analyze/async",
            json={
                "query": "重新分析",
                "user_id": "u1",
                "session_id": first["session_id"],
                "regenerate_of": first["assistant_message_id"],
                "auto_persist": True,
            },
        )
        assert resp.status_code == 202
        ack = resp.json()
        assert ack["assistant_message_id"] == first["assistant_message_id"]

        entry2 = registry.get(ack["trace_id"])
        await asyncio.wait_for(entry2.task, timeout=2.0)  # type: ignore[arg-type]

        session = await asyncio.to_thread(
            chat_repo.get_session_with_messages,
            "u1", first["session_id"], 50,
        )
        # 重新生成不新增消息，总共还是 2 条
        assert len(session["messages"]) == 2
        asst = session["messages"][-1]
        assert asst["id"] == first["assistant_message_id"]
        assert asst["status"] == "success"


@pytest.mark.asyncio
async def test_snapshot_fallback_to_trace_runs(app_env) -> None:
    """TTL 过期后内存 entry 被清理，快照应能从 trace_runs 构造。"""
    fastapi_app, _fake, _chat_repo, registry = app_env
    # 构造一条 trace_runs 记录
    from core.observability.tables import TraceRun
    from core.time_utils import now_cn

    with registry._session_factory() as s:  # noqa: SLF001
        s.add(TraceRun(
            trace_id="fallback-trace",
            agent_name="p2p",
            session_id="s-fb",
            user_id="u1",
            status="success",
            started_at=now_cn(),
            finished_at=now_cn(),
            duration_ms=123.0,
        ))
        s.commit()

    async with _make_client(fastapi_app) as client:
        resp = await client.get("/analyze/tasks/fallback-trace")
        assert resp.status_code == 200
        snap = resp.json()
        assert snap["trace_id"] == "fallback-trace"
        assert snap["status"] == "ok"
        assert snap["duration_ms"] == 123.0


@pytest.mark.asyncio
async def test_heartbeat_has_ts_and_seq(app_env, monkeypatch) -> None:
    """心跳也必须带 type/trace_id/ts/seq 四个基础字段（前端契约）。"""
    import api.routes.analyze_async as ar

    monkeypatch.setattr(ar, "_sse_heartbeat_seconds", lambda: 0.05)

    # 让 runner 多跑一会儿，以便触发 heartbeat
    async def slow_runner(req, *, trace_id=None):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.3)
        return AnalysisResult(
            report_id="r", trace_id=trace_id or "",
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query=req.query, user_id=req.user_id, session_id=req.session_id,
            time_range="30d", report_markdown="# ok",
        )

    fastapi_app, _fake, _chat_repo, _registry = app_env
    slow_orch = type("SO", (), {"analyze": staticmethod(slow_runner)})()
    monkeypatch.setattr(
        async_module, "_get_orchestrator", lambda: slow_orch, raising=True
    )
    async with _make_client(fastapi_app) as client:
        ack = await _post_submit(client, query="heartbeat 字段")
        heartbeats: list[dict[str, Any]] = []
        async with client.stream("GET", ack["stream_url"]) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[len("data: "):])
                    if ev.get("type") == "heartbeat":
                        heartbeats.append(ev)
                    if ev.get("type") == "done":
                        break
        assert heartbeats, "应至少触发一条 heartbeat"
        h0 = heartbeats[0]
        assert h0["type"] == "heartbeat"
        assert h0["trace_id"] == ack["trace_id"]
        # heartbeat 不占用业务 seq 计数器，恒为 0（与业务事件区分）
        assert h0.get("seq") == 0
        assert "ts" in h0 and isinstance(h0["ts"], str)


@pytest.mark.asyncio
async def test_sse_stream_delivers_done(app_env) -> None:
    fastapi_app, *_ = app_env
    async with _make_client(fastapi_app) as client:
        ack = await _post_submit(client)
        trace_id = ack["trace_id"]

        events: list[dict[str, Any]] = []
        async with client.stream(
            "GET", f"/analyze/tasks/{trace_id}/events"
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))
                    if events[-1].get("type") == "done":
                        break

        types = [e["type"] for e in events]
        assert "status" in types
        assert events[-1]["type"] == "done"
        assert events[-1]["status"] == "ok"


# ---------------------------------------------------------------------------
# 健壮性三道闸（详见 docs/issues/async_analyze_backend_issue.md）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_404_for_unknown_trace(app_env) -> None:
    """闸 1：未提交过的 trace_id 握手时直接返回 404，不进入 SSE 循环。"""
    fastapi_app, *_ = app_env
    async with _make_client(fastapi_app) as client:
        resp = await client.get("/analyze/tasks/unknown-trace-xxx/events")
        assert resp.status_code == 404
        assert "不存在" in resp.text


@pytest.mark.asyncio
async def test_sse_first_frame_is_synthesized_status(app_env) -> None:
    """闸 2：SSE 连接建立后的第一帧必须是 synthesized 的 status 快照，
    不依赖 buffer replay。这保证 buffer 已 drop / Redis 异常等场景下
    前端至少能收到一帧当前状态。"""
    fastapi_app, *_ = app_env
    async with _make_client(fastapi_app) as client:
        ack = await _post_submit(client)
        trace_id = ack["trace_id"]

        first_event: dict[str, Any] | None = None
        async with client.stream(
            "GET", f"/analyze/tasks/{trace_id}/events"
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    first_event = json.loads(line[len("data: "):])
                    break

        assert first_event is not None
        assert first_event["type"] == "status"
        assert first_event["trace_id"] == trace_id
        assert first_event.get("synthesized") is True
        # 合成帧 seq=0，不占用业务 seq 计数器
        assert first_event.get("seq") == 0
        assert first_event.get("state") in {"queued", "running", "ok"}


@pytest.mark.asyncio
async def test_sse_synthesizes_done_for_terminal_trace_without_buffer(
    app_env,
) -> None:
    """闸 2 + 3：任务已终态但 registry / bus 里没有事件（模拟 buffer 已过期）时，
    SSE 仍要从 trace_runs 合成 status + done 两帧后关流，而不是空响应。"""
    fastapi_app, _fake, _chat_repo, registry = app_env

    # 构造一条 trace_runs 记录，但不在 registry 中登记
    # （等价于 entry TTL 过期后的状态）
    from core.observability.tables import TraceRun
    from core.time_utils import now_cn

    trace_id = "terminal-without-buffer"
    with registry._session_factory() as s:  # noqa: SLF001
        s.add(TraceRun(
            trace_id=trace_id,
            agent_name="p2p",
            session_id="s-gate3",
            user_id="u1",
            status="success",
            started_at=now_cn(),
            finished_at=now_cn(),
            duration_ms=456.0,
        ))
        s.commit()

    async with _make_client(fastapi_app) as client:
        events: list[dict[str, Any]] = []
        async with client.stream(
            "GET", f"/analyze/tasks/{trace_id}/events"
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))

        # 至少要有 status + done 两帧；最后一帧必须是 done
        assert len(events) >= 2, f"got only {events}"
        assert events[0]["type"] == "status"
        assert events[0].get("synthesized") is True
        assert events[0]["state"] == "ok"
        assert events[-1]["type"] == "done"
        assert events[-1].get("synthesized") is True
        assert events[-1]["status"] == "ok"
        assert events[-1]["duration_ms"] == 456.0


@pytest.mark.asyncio
async def test_registry_run_waits_for_trace_store_flush_before_done(
    app_env, monkeypatch
) -> None:
    """registry._run 必须在 publish_done 之前调用 TraceStore.flush_now_sync，
    这样前端 SSE 收到 done 时 DB 的 trace_runs 一定已经是终态。

    用 monkeypatch 记录 flush_now_sync 的调用时刻和 publish_done 的调用时刻，
    断言前者严格早于后者。
    """
    import time

    import core.observability.store as store_module
    import core.tasks.registry as registry_module

    fastapi_app, _fake, _chat_repo, registry = app_env

    timeline: list[tuple[str, float]] = []
    real_store = store_module.get_trace_store()
    assert real_store is not None, "TraceStore 应已在 app_env 初始化"

    original_flush = real_store.flush_now_sync

    def recording_flush(trace_id: str, *, timeout: float = 2.0) -> bool:
        timeline.append(("flush_now_sync", time.monotonic()))
        return original_flush(trace_id, timeout=timeout)

    monkeypatch.setattr(
        real_store, "flush_now_sync", recording_flush, raising=True
    )

    original_publish_done = registry_module.TaskRegistry._publish_done

    def recording_publish_done(self, entry) -> None:  # type: ignore[no-untyped-def]
        timeline.append(("publish_done", time.monotonic()))
        return original_publish_done(self, entry)

    monkeypatch.setattr(
        registry_module.TaskRegistry,
        "_publish_done",
        recording_publish_done,
        raising=True,
    )

    async with _make_client(fastapi_app) as client:
        ack = await _post_submit(client, query="flush barrier 验证")
        entry = registry.get(ack["trace_id"])
        assert entry is not None and entry.task is not None
        await asyncio.wait_for(entry.task, timeout=3.0)

    # 必须两个都调到了，且 flush 先于 publish
    kinds = [k for k, _ in timeline]
    assert "flush_now_sync" in kinds, "registry._run 未调用 flush_now_sync"
    assert "publish_done" in kinds
    flush_t = next(t for k, t in timeline if k == "flush_now_sync")
    done_t = next(t for k, t in timeline if k == "publish_done")
    assert flush_t < done_t, (
        f"flush_now_sync 必须早于 publish_done；实际 timeline={timeline}"
    )


@pytest.mark.asyncio
async def test_sse_synthesizes_done_for_terminal_error_trace(app_env) -> None:
    """闸 3：终态为 error 时 done 帧也能正确合成并带上错误信息。"""
    fastapi_app, _fake, _chat_repo, registry = app_env
    from core.observability.tables import TraceRun
    from core.time_utils import now_cn

    trace_id = "terminal-error-trace"
    with registry._session_factory() as s:  # noqa: SLF001
        s.add(TraceRun(
            trace_id=trace_id,
            agent_name="p2p",
            session_id="s-gate3e",
            user_id="u1",
            status="error",
            started_at=now_cn(),
            finished_at=now_cn(),
            duration_ms=12.0,
            error="上游超时",
        ))
        s.commit()

    async with _make_client(fastapi_app) as client:
        events: list[dict[str, Any]] = []
        async with client.stream(
            "GET", f"/analyze/tasks/{trace_id}/events"
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))

        assert events[-1]["type"] == "done"
        assert events[-1]["status"] == "error"
        assert events[-1].get("error") is not None
        assert "上游超时" in events[-1]["error"]["message"]


# ---------------------------------------------------------------------------
# 跨 worker POST → GET race：Seed trace_runs 保证 Gate 1 不误伤 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_seeds_trace_runs_row_synchronously(app_env) -> None:
    """POST /analyze/async 返回后，trace_runs 必须立刻有一行 status=queued，
    而不用等 orchestrator 后台 start_run 的批量 flush。

    前端如果在 POST 返回后 < 1s 内查快照（尤其跨 worker 场景回落 DB），
    必须能查到这行种子记录，避免被 Gate 1 打成 404。
    """
    from core.observability.tables import TraceRun

    fastapi_app, _fake, _chat_repo, registry = app_env

    async with _make_client(fastapi_app) as client:
        ack = await _post_submit(client, query="seed 验证")
        trace_id = ack["trace_id"]

        # POST 一返回就同步查 DB，不等后台任务
        with registry._session_factory() as s:  # noqa: SLF001
            row: TraceRun | None = s.get(TraceRun, trace_id)
        assert row is not None, "submit() 必须在返回前同步 seed trace_runs"
        assert row.status == "queued"
        assert row.session_id == ack["session_id"]
        assert row.user_id == "u1"
        assert row.started_at is not None


@pytest.mark.asyncio
async def test_sse_events_endpoint_no_404_when_registry_misses_but_db_has_seed(
    app_env,
) -> None:
    """模拟跨 worker：POST 落 Worker A（登记了 entry），前端立刻 GET /events
    落到 Worker B（registry 里没这个 trace_id）。Gate 1 应能通过 DB seed 放行，
    而不是 404。

    构造方式：POST 后手工把 registry 里的 entry 删掉，模拟 Worker B 的视角。
    EventBus 是跨 worker 共享的，所以后续订阅仍然工作。
    """
    fastapi_app, _fake, _chat_repo, registry = app_env

    async with _make_client(fastapi_app) as client:
        ack = await _post_submit(client, query="跨 worker 模拟")
        trace_id = ack["trace_id"]

        # 等一下让后台 task 跑起来但不等完
        await asyncio.sleep(0.01)

        # 模拟 Worker B：清掉 registry 的内存 entry，这样 _resolve_task_snapshot
        # 只能回落到 DB（trace_runs）
        registry._entries.pop(trace_id, None)  # noqa: SLF001

        # 立刻 GET /events：必须不是 404
        async with client.stream(
            "GET", f"/analyze/tasks/{trace_id}/events"
        ) as resp:
            assert resp.status_code == 200, (
                f"Gate 1 不应把有 seed 的 trace_id 打成 404；实际 status={resp.status_code} "
                f"body={await resp.aread()!r}"
            )
            # 应当收到合成的 status 首帧（state 可能是 queued/running/ok，取决于时序）
            first: dict[str, Any] | None = None
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    first = json.loads(line[len("data: "):])
                    break
            assert first is not None
            assert first["type"] == "status"
            assert first.get("synthesized") is True
            assert first["state"] in {"queued", "running", "ok"}


# ---------------------------------------------------------------------------
# 跨 worker GET /tasks/{trace_id}：reports 表反查 result hydrate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_worker_snapshot_includes_full_result(
    app_env, monkeypatch
) -> None:
    """跨 worker 场景：registry 里没有 entry，DB 有 trace_runs + reports，
    快照必须把 reports.result_json 反序列化后放进 snapshot.result。

    复现前端报告的 "status=ok 但 result=null" bug 的反向：修复后必须有 result。
    """
    from api.schemas.analysis import AnalysisResult, AnalysisStatus, AnalysisType
    from core.observability.tables import TraceRun
    from core.time_utils import now_cn

    fastapi_app, _fake, _chat_repo, registry = app_env

    trace_id = "cross-worker-trace"
    # 1. trace_runs 里写入终态成功行
    with registry._session_factory() as s:  # noqa: SLF001
        s.add(TraceRun(
            trace_id=trace_id,
            agent_name="p2p_agent",
            session_id="s-cross",
            user_id="u1",
            status="success",
            started_at=now_cn(),
            finished_at=now_cn(),
            duration_ms=1234.5,
        ))
        s.commit()

    # 2. Mock _hydrate_result_from_reports 返回完整 AnalysisResult。
    #    真实链路是 orchestrator._persist_report → reports 表 → ReportRepository.get_by_trace_id，
    #    这里的 monkeypatch 等价于那条链路成功 hydrate 的结果。
    fake_result = AnalysisResult(
        report_id="r-1",
        trace_id=trace_id,
        status=AnalysisStatus.SUCCESS,
        analysis_type=AnalysisType.COMPREHENSIVE,
        query="cross-worker 验证",
        user_id="u1",
        session_id="s-cross",
        time_range="30d",
        report_markdown="# 成功报告\n内容完整",
        duration_ms=1234.5,
    )
    monkeypatch.setattr(
        async_module, "_hydrate_result_from_reports",
        lambda tid: fake_result if tid == trace_id else None,
        raising=True,
    )

    async with _make_client(fastapi_app) as client:
        resp = await client.get(f"/analyze/tasks/{trace_id}")
        assert resp.status_code == 200
        snap = resp.json()
        assert snap["status"] == "ok"
        # 核心断言：result 必须是对象，不能是 null
        assert snap["result"] is not None, (
            "前端报告的 bug：status=ok 但 result=null。"
            "修复后 _load_snapshot_from_db 必须从 reports 表 hydrate result"
        )
        assert snap["result"]["report_markdown"] == "# 成功报告\n内容完整"
        assert snap["result"]["trace_id"] == trace_id


@pytest.mark.asyncio
async def test_cross_worker_snapshot_degrades_when_reports_row_missing(
    app_env, monkeypatch
) -> None:
    """_persist_report 失败或 is_recall 场景：reports 表没对应行时，
    snapshot 应该返回 status=ok、result=null，但不 500、不 hang。

    前端这时会走 "3 次 × 1s 重试" 兜底并最终展示占位文案；
    这是合理的降级，不属于 bug。
    """
    from core.observability.tables import TraceRun
    from core.time_utils import now_cn

    fastapi_app, _fake, _chat_repo, registry = app_env

    trace_id = "missing-reports-trace"
    with registry._session_factory() as s:  # noqa: SLF001
        s.add(TraceRun(
            trace_id=trace_id,
            agent_name="p2p_agent",
            session_id="s-miss",
            user_id="u1",
            status="success",
            started_at=now_cn(),
            finished_at=now_cn(),
            duration_ms=42.0,
        ))
        s.commit()

    # 模拟 reports 表没有这个 trace_id（或 _persist_report 失败过）
    monkeypatch.setattr(
        async_module, "_hydrate_result_from_reports",
        lambda tid: None,
        raising=True,
    )

    async with _make_client(fastapi_app) as client:
        resp = await client.get(f"/analyze/tasks/{trace_id}")
        assert resp.status_code == 200
        snap = resp.json()
        assert snap["status"] == "ok"
        assert snap["duration_ms"] == 42.0
        # 优雅降级：result 可以是 null，但绝不能 500
        assert snap["result"] is None
        assert snap["error"] is None


@pytest.mark.asyncio
async def test_snapshot_endpoint_maps_queued_status_correctly(app_env) -> None:
    """_trace_status_to_task_state 必须把 DB 的 'queued' 映射回 TaskState.QUEUED，
    而不是默认 ERROR（否则前端会把排队中的任务当成失败）。
    """
    fastapi_app, _fake, _chat_repo, registry = app_env

    async with _make_client(fastapi_app) as client:
        ack = await _post_submit(client, query="queued 映射")
        trace_id = ack["trace_id"]

        # 再次模拟 Worker B 视角：清掉内存 entry，强制走 DB 回落
        await asyncio.sleep(0.01)
        registry._entries.pop(trace_id, None)  # noqa: SLF001

        resp = await client.get(f"/analyze/tasks/{trace_id}")
        assert resp.status_code == 200
        snap = resp.json()
        # DB seed 行刚入库状态应为 queued（后台 task 还没来得及把它 merge 成 running）
        assert snap["status"] in {"queued", "running", "ok"}
        # 关键：绝不能是 "error"（queued 映射缺失时的症状）
        assert snap["status"] != "error"
