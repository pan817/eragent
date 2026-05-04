"""ReAct 路径 SSE chunk 事件端到端集成测试（Phase 2）。

验证 ``node="agent_final"`` 的 ChunkEvent 能正确流过：

1. P2PAgent 发布 → MemoryEventBus（ephemeral=True，不入 buffer）
2. SSE 端点透传 → HTTP `data:` 行
3. 客户端按 ChunkEvent 协议解析（type / node / message_id / index / eos）

不跑真实 LLM，用 FakeOrchestrator 直接调用 EventBus.publish 模拟 ReAct 推送。
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
from core.database import get_session_factory, init_database
from core.database.engine import create_engine_from_dsn
from core.tasks.events import get_event_bus


class _ChunkPublishingOrchestrator:
    """模拟 ReAct 路径的 Orchestrator：在 analyze() 内调用 EventBus 发 chunk 事件，
    然后返回完整 AnalysisResult，触发 done。

    用于验证 SSE 端点对 chunk 事件的透传与 ChunkEvent 协议合规性。
    """

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.full_text = "".join(chunks)

    async def analyze(self, request, *, trace_id: str | None = None):  # type: ignore[no-untyped-def]
        bus = get_event_bus()
        msg_id = trace_id or "fallback"
        # 给 SSE client 留出连接窗口（真实场景下前端总是在 ack 后立即建连）
        await asyncio.sleep(0.15)
        # 模拟 _astream_react_with_publish 推送 chunk
        for idx, delta in enumerate(self._chunks):
            bus.publish(
                trace_id or "",
                {
                    "type": "chunk",
                    "trace_id": trace_id or "",
                    "ts": "2026-04-16T16:00:00+08:00",
                    "seq": 0,
                    "node": "agent_final",
                    "message_id": msg_id,
                    "delta": delta,
                    "index": idx,
                    "eos": False,
                },
                ephemeral=True,
            )
            # 让出 loop，模拟 token 节奏
            await asyncio.sleep(0.005)
        # 末帧 eos
        bus.publish(
            trace_id or "",
            {
                "type": "chunk",
                "trace_id": trace_id or "",
                "ts": "2026-04-16T16:00:01+08:00",
                "seq": 0,
                "node": "agent_final",
                "message_id": msg_id,
                "delta": "",
                "index": len(self._chunks),
                "eos": True,
            },
            ephemeral=True,
        )
        return AnalysisResult(
            report_id="r1",
            trace_id=trace_id or "",
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query=request.query,
            user_id=request.user_id,
            session_id=request.session_id,
            time_range="30d",
            report_markdown=self.full_text,
            duration_ms=20.0,
        )


@pytest_asyncio.fixture()
async def streaming_app(monkeypatch):
    """启动一个最小可运行的 FastAPI app，注入 chunk 推送 orchestrator。"""
    engine = create_engine_from_dsn(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from modules.p2p.mock_data.generator import MockDataGenerator

    init_database(engine, seed=0, data_generator_factory=MockDataGenerator)
    session_factory = get_session_factory(engine)

    chunks = [
        "## ReAct 报告\n\n",
        "本次分析覆盖 ",
        "**最近 30 天** 的采购数据。",
    ]
    fake = _ChunkPublishingOrchestrator(chunks)
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
        mock_cfg.erp_schema = "oracle_ebs"
        mock_cfg.postgresql = MagicMock()
        mock_cfg.async_analysis = AsyncAnalysisSettings(
            max_concurrent_tasks=4,
            result_cache_ttl_sec=60,
            sweep_interval_sec=60,
            event_buffer_size=50,
            sse_heartbeat_sec=1,
            trace_flush_barrier_timeout=0.1,
        )
        mock_settings_fn.return_value = mock_cfg

        from api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            async with app.router.lifespan_context(app):
                yield client, fake, chunks

    engine.dispose()


@pytest.mark.asyncio
async def test_react_chunks_delivered_via_sse(streaming_app) -> None:
    """ReAct chunks 通过 SSE 透传给客户端，按 index 递增、末帧 eos=true。"""
    client, fake, chunks = streaming_app

    resp = await client.post(
        "/api/v1/ptp-agent/analyze/async",
        json={"query": "react sse", "user_id": "u1", "auto_persist": False},
    )
    assert resp.status_code == 202, resp.text
    ack = resp.json()
    trace_id = ack["trace_id"]

    events: list[dict[str, Any]] = []
    async with client.stream("GET", ack["stream_url"]) as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
                if events[-1].get("type") == "done":
                    break

    # 提取 chunk 事件
    chunk_events = [e for e in events if e.get("type") == "chunk"]
    assert chunk_events, f"应至少收到 1 个 chunk 事件; events={events}"

    # 全部 node="agent_final"
    assert all(c["node"] == "agent_final" for c in chunk_events)
    # 全部 seq=0
    assert all(c["seq"] == 0 for c in chunk_events)
    # message_id 一致（绑定同一个气泡）
    msg_ids = {c["message_id"] for c in chunk_events}
    assert len(msg_ids) == 1, f"message_id 应唯一: {msg_ids}"
    # index 严格递增
    indices = [c["index"] for c in chunk_events]
    assert indices == sorted(indices), f"index 必须递增: {indices}"
    # 末帧 eos=true
    assert chunk_events[-1]["eos"] is True

    # delta 拼接 == 完整文本
    accumulated = "".join(c["delta"] for c in chunk_events)
    assert accumulated == "".join(chunks)

    # done 事件在所有 chunk 之后
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "ok"


@pytest.mark.asyncio
async def test_react_chunks_not_in_replay_buffer(streaming_app) -> None:
    """chunk 事件 ephemeral=True，不进 ring buffer。

    验证方式：等任务结束 + EventBus 标记 trace 已 close 后再订阅，
    应**只**收到非 chunk 的持久事件（status / done 等），没有 chunk。
    """
    client, fake, chunks = streaming_app

    resp = await client.post(
        "/api/v1/ptp-agent/analyze/async",
        json={"query": "ephemeral check", "user_id": "u1", "auto_persist": False},
    )
    ack = resp.json()
    trace_id = ack["trace_id"]

    # 等任务跑完
    from core.tasks import get_task_registry

    registry = get_task_registry()
    assert registry is not None
    entry = registry.get(trace_id)
    assert entry is not None and entry.task is not None
    await asyncio.wait_for(entry.task, timeout=2.0)

    # 重新订阅（断线重连模拟）：buffer 里不应有 chunk
    bus = get_event_bus()
    replayed = list(bus.buffered(trace_id))
    chunk_in_buffer = [e for e in replayed if e.get("type") == "chunk"]
    assert chunk_in_buffer == [], (
        f"chunk 事件不应进入 ring buffer (ephemeral=True); 实际: {chunk_in_buffer}"
    )
    # 持久事件应该有 status / done 等
    persistent_types = {e.get("type") for e in replayed}
    assert "done" in persistent_types or "status" in persistent_types
