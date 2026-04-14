"""异步分析接口集成测试。

覆盖点：
- lifespan 内 EventBus / TaskRegistry 正确初始化
- 三个端点通过真实 FastAPI app 可访问（带路由 prefix）
- chat_messages pending → success 的完整状态流转
- SSE 事件流能收到 status / done
- 进程"重启"模拟：recover_on_startup 把 pending 消息标记为 error

不跑真实 LLM，用 FakeOrchestrator 注入。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool

import api.routes.analyze_async as async_module
from api.schemas.analysis import (
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
)
from core.chat.tables import chat_messages_table, chat_sessions_table
from core.database import init_database, get_session_factory
from core.database.engine import create_engine_from_dsn


class _FakeOrchestrator:
    def __init__(self, *, delay: float = 0.02) -> None:
        self.delay = delay

    async def analyze(self, request, *, trace_id: str | None = None):  # type: ignore[no-untyped-def]
        await asyncio.sleep(self.delay)
        return AnalysisResult(
            report_id="r1",
            trace_id=trace_id or "",
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query=request.query,
            user_id=request.user_id,
            session_id=request.session_id,
            time_range="30d",
            report_markdown="# 集成测试报告",
            duration_ms=20.0,
        )


@pytest_asyncio.fixture()
async def running_app(monkeypatch):
    """用 SQLite 构造一个经过 lifespan 启动的 FastAPI 应用。"""
    engine = create_engine_from_dsn(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_database(engine, seed=0)
    session_factory = get_session_factory(engine)

    fake = _FakeOrchestrator()
    monkeypatch.setattr(
        async_module, "_get_orchestrator", lambda: fake, raising=True
    )

    with (
        patch("api.main.get_settings") as mock_settings_fn,
        patch("api.main.get_engine", return_value=engine),
        patch("api.main.create_tables"),
        patch("api.main.get_session_factory", return_value=session_factory),
    ):
        from config.settings import AsyncAnalysisSettings

        mock_cfg = MagicMock()
        mock_cfg.app_name = "ERP Agent Test"
        mock_cfg.app_version = "0.1.0-test"
        mock_cfg.postgresql = MagicMock()
        # 使用更短的 TTL 和心跳方便测试
        mock_cfg.async_analysis = AsyncAnalysisSettings(
            max_concurrent_tasks=4,
            result_cache_ttl_sec=60,
            sweep_interval_sec=60,
            event_buffer_size=50,
            sse_heartbeat_sec=1,
            # FakeOrchestrator 不走 TimingMiddleware，run_end 永远不会入队；
            # 让 barrier 很快超时降级 WARNING 放行，避免每个测试浪费 2s 挂在 barrier。
            trace_flush_barrier_timeout=0.1,
        )
        mock_settings_fn.return_value = mock_cfg

        from api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # 触发 lifespan startup
            async with app.router.lifespan_context(app):
                yield client, session_factory, fake

    engine.dispose()


@pytest.mark.asyncio
async def test_submit_and_fetch_final_result(running_app) -> None:
    client, session_factory, _fake = running_app
    resp = await client.post(
        "/api/v1/ptp-agent/analyze/async",
        json={"query": "集成测试", "user_id": "u1", "auto_persist": True},
    )
    assert resp.status_code == 202, resp.text
    ack = resp.json()
    assert ack["trace_id"]
    assert ack["poll_url"].startswith("/api/v1/ptp-agent/analyze/tasks/")
    assert ack["stream_url"].endswith("/events")

    # 等待后台任务完成
    from core.tasks import get_task_registry

    registry = get_task_registry()
    assert registry is not None
    entry = registry.get(ack["trace_id"])
    assert entry is not None and entry.task is not None
    await asyncio.wait_for(entry.task, timeout=2.0)

    snap = (await client.get(ack["poll_url"])).json()
    assert snap["status"] == "ok"
    assert snap["result"]["report_markdown"] == "# 集成测试报告"

    # 消息状态检查
    with session_factory() as s:
        msgs = list(s.execute(
            chat_messages_table.select().where(
                chat_messages_table.c.session_id == ack["session_id"]
            )
        ).fetchall())
    assert {m.role: m.status for m in msgs} == {
        "user": "success",
        "assistant": "success",
    }


@pytest.mark.asyncio
async def test_sse_stream_integration(running_app) -> None:
    client, *_ = running_app
    resp = await client.post(
        "/api/v1/ptp-agent/analyze/async",
        json={"query": "sse 集成", "user_id": "u1", "auto_persist": False},
    )
    ack = resp.json()
    events: list[dict[str, Any]] = []
    async with client.stream("GET", ack["stream_url"]) as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
                if events[-1].get("type") == "done":
                    break
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "ok"


@pytest.mark.asyncio
async def test_recover_on_startup_marks_pending_as_error(
    running_app,
) -> None:
    """模拟"上次进程残留的 pending 消息"，新一轮 startup 应将其标记为 error。"""
    client, session_factory, _fake = running_app
    # 手动塞一条 pending 消息
    with session_factory() as s:
        s.execute(chat_sessions_table.insert().values(
            id="stale-sess", user_id="u1", title="旧会话",
            title_auto=True, message_count=1,
        ))
        s.execute(chat_messages_table.insert().values(
            id="m-stale", session_id="stale-sess", role="assistant",
            content="", status="pending", trace_id="stale",
        ))
        s.commit()
    # 再次调用 recover（模拟新一次 startup 的幂等执行）
    from core.tasks import get_task_registry

    registry = get_task_registry()
    assert registry is not None
    registry.recover_on_startup()

    with session_factory() as s:
        msg = s.execute(
            chat_messages_table.select().where(
                chat_messages_table.c.id == "m-stale"
            )
        ).fetchone()
    assert msg is not None and msg.status == "error"
