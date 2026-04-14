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
        assert isinstance(h0.get("seq"), int) and h0["seq"] > 0
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
