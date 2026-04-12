"""api/routes/traces.py + api/routes/analyze.py 单元测试。

使用 FastAPI TestClient + unittest.mock 隔离外部依赖，不需要真实 DB。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

NOW = datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Traces routes
# ---------------------------------------------------------------------------


def _make_trace_app():
    """建立仅挂 traces 路由的小型 FastAPI 应用。"""
    from fastapi import FastAPI
    from api.routes.traces import router

    app = FastAPI()
    app.include_router(router)
    return app


def _fake_run(**kwargs) -> MagicMock:
    run = MagicMock(spec=[])
    run.trace_id = kwargs.get("trace_id", str(uuid.uuid4()))
    run.agent_name = kwargs.get("agent_name", "p2p_agent")
    run.session_id = kwargs.get("session_id", "sess1")
    run.user_id = kwargs.get("user_id", "u1")
    run.status = kwargs.get("status", "ok")
    run.started_at = NOW
    run.finished_at = NOW
    run.duration_ms = 100.0
    run.model_call_count = 1
    run.tool_call_count = 2
    run.error = None
    run.token_summary = None
    return run


def _fake_span(**kwargs) -> MagicMock:
    sp = MagicMock()
    sp.span_id = str(uuid.uuid4())
    sp.trace_id = kwargs.get("trace_id", str(uuid.uuid4()))
    sp.parent_span_id = None
    sp.span_type = kwargs.get("span_type", "model")
    sp.name = kwargs.get("name", "llm")
    sp.status = "ok"
    sp.started_at = NOW
    sp.finished_at = NOW
    sp.duration_ms = 10.0
    sp.attributes = kwargs.get("attributes", {"input": [], "output": {}})
    sp.error = None
    return sp


@pytest.fixture()
def traces_client():
    app = _make_trace_app()
    return TestClient(app)


class TestTracesRouter:
    def test_list_traces_store_not_initialized(self, traces_client):
        with patch("api.routes.traces.get_trace_store", return_value=None):
            resp = traces_client.get("/traces")
        assert resp.status_code == 503

    def test_list_traces_ok(self, traces_client):
        run = _fake_run()
        mock_store = MagicMock()
        mock_store.list_runs.return_value = [run]
        mock_store.get_token_summary.return_value = None
        with patch("api.routes.traces.get_trace_store", return_value=mock_store):
            resp = traces_client.get("/traces")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["agent_name"] == "p2p_agent"

    def test_list_traces_with_token_summary(self, traces_client):
        run = _fake_run()
        mock_store = MagicMock()
        mock_store.list_runs.return_value = [run]
        mock_store.get_token_summary.return_value = {
            "total_prompt_tokens": 5000,
            "total_completion_tokens": 1000,
            "peak_prompt_tokens": 3000,
        }
        with patch("api.routes.traces.get_trace_store", return_value=mock_store):
            resp = traces_client.get("/traces")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["token_summary"]["total_prompt_tokens"] == 5000

    def test_list_traces_with_filters(self, traces_client):
        mock_store = MagicMock()
        mock_store.list_runs.return_value = []
        mock_store.get_token_summary.return_value = None
        with patch("api.routes.traces.get_trace_store", return_value=mock_store):
            resp = traces_client.get("/traces?session_id=sess1&user_id=u1&limit=10&offset=5")
        assert resp.status_code == 200
        mock_store.list_runs.assert_called_once()

    def test_trace_stats_store_not_initialized(self, traces_client):
        with patch("api.routes.traces.get_trace_store", return_value=None):
            resp = traces_client.get("/traces/stats")
        assert resp.status_code == 503

    def test_trace_stats_ok(self, traces_client):
        mock_store = MagicMock()
        mock_store.stats.return_value = [
            {
                "key": "tool_a",
                "span_type": "tool",
                "count": 3,
                "avg_ms": 50.0,
                "p50_ms": 45.0,
                "p95_ms": 90.0,
                "min_ms": 20.0,
                "max_ms": 100.0,
            }
        ]
        with patch("api.routes.traces.get_trace_store", return_value=mock_store):
            resp = traces_client.get("/traces/stats?group_by=tool_name")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["key"] == "tool_a"

    def test_trace_stats_invalid_group_by(self, traces_client):
        mock_store = MagicMock()
        with patch("api.routes.traces.get_trace_store", return_value=mock_store):
            resp = traces_client.get("/traces/stats?group_by=invalid")
        assert resp.status_code == 422

    def test_get_trace_io_not_found(self, traces_client):
        mock_store = MagicMock()
        mock_store.get_run.return_value = None
        with patch("api.routes.traces.get_trace_store", return_value=mock_store):
            resp = traces_client.get("/traces/nonexistent/io")
        assert resp.status_code == 404

    def test_get_trace_io_ok(self, traces_client):
        trace_id = str(uuid.uuid4())
        run = _fake_run(trace_id=trace_id)
        sp_model = _fake_span(trace_id=trace_id, span_type="model", name="llm")
        sp_tool = _fake_span(trace_id=trace_id, span_type="tool", name="tool_a")
        sp_other = _fake_span(trace_id=trace_id, span_type="checkpoint", name="ckpt")
        mock_store = MagicMock()
        mock_store.get_run.return_value = (run, [sp_model, sp_tool, sp_other])
        with patch("api.routes.traces.get_trace_store", return_value=mock_store):
            resp = traces_client.get(f"/traces/{trace_id}/io")
        assert resp.status_code == 200
        data = resp.json()
        # checkpoint span should be excluded
        assert len(data) == 2

    def test_get_trace_io_filter_by_type(self, traces_client):
        trace_id = str(uuid.uuid4())
        run = _fake_run(trace_id=trace_id)
        sp_model = _fake_span(trace_id=trace_id, span_type="model")
        sp_tool = _fake_span(trace_id=trace_id, span_type="tool")
        mock_store = MagicMock()
        mock_store.get_run.return_value = (run, [sp_model, sp_tool])
        with patch("api.routes.traces.get_trace_store", return_value=mock_store):
            resp = traces_client.get(f"/traces/{trace_id}/io?span_type=tool")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["span_type"] == "tool"

    def test_get_trace_detail_not_found(self, traces_client):
        mock_store = MagicMock()
        mock_store.get_run.return_value = None
        with patch("api.routes.traces.get_trace_store", return_value=mock_store):
            resp = traces_client.get("/traces/missing-id")
        assert resp.status_code == 404

    def test_get_trace_detail_ok(self, traces_client):
        trace_id = str(uuid.uuid4())
        run = _fake_run(trace_id=trace_id)
        sp = _fake_span(trace_id=trace_id)
        mock_store = MagicMock()
        mock_store.get_run.return_value = (run, [sp])
        mock_store.get_token_summary.return_value = {
            "total_prompt_tokens": 2000,
            "total_completion_tokens": 500,
            "peak_prompt_tokens": 2000,
        }
        with patch("api.routes.traces.get_trace_store", return_value=mock_store):
            resp = traces_client.get(f"/traces/{trace_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["trace_id"] == trace_id
        assert len(data["spans"]) == 1
        assert data["token_summary"]["total_prompt_tokens"] == 2000


# ---------------------------------------------------------------------------
# Analyze routes
# ---------------------------------------------------------------------------


def _make_analyze_app():
    from fastapi import FastAPI
    from api.routes.analyze import router

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def analyze_client():
    app = _make_analyze_app()
    return TestClient(app, raise_server_exceptions=False)


class TestAnalyzeRouter:
    def test_analyze_returns_result(self, analyze_client):
        from api.schemas.analysis import (
            AnalysisResult,
            AnalysisStatus,
            AnalysisType,
        )

        mock_result = AnalysisResult(
            report_id=str(uuid.uuid4()),
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.THREE_WAY_MATCH,
            query="三路匹配",
            user_id="u1",
            session_id="sess1",
            time_range="2026-01-01/2026-04-01",
            anomalies=[],
        )

        mock_orchestrator = MagicMock()
        mock_orchestrator.analyze = AsyncMock(return_value=mock_result)

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orchestrator),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = analyze_client.post(
                "/analyze",
                json={"query": "三路匹配", "user_id": "u1"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["query"] == "三路匹配"

    def test_analyze_auto_generates_session_id(self, analyze_client):
        from api.schemas.analysis import (
            AnalysisResult,
            AnalysisStatus,
            AnalysisType,
        )
        captured: list[Any] = []

        async def capture(req):
            captured.append(req.session_id)
            return AnalysisResult(
                report_id=str(uuid.uuid4()),
                status=AnalysisStatus.SUCCESS,
                analysis_type=AnalysisType.COMPREHENSIVE,
                query=req.query,
                user_id=req.user_id,
                session_id=req.session_id,
                time_range="",
            )

        mock_orch = MagicMock()
        mock_orch.analyze = capture

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = analyze_client.post(
                "/analyze",
                json={"query": "供应商绩效"},
            )
        assert resp.status_code == 200
        # session_id 应被自动填充为 UUID 格式
        assert captured[0] != ""
        assert len(captured[0]) == 36  # UUID str length

    def test_analyze_exception_returns_failed_result(self, analyze_client):
        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = analyze_client.post("/analyze", json={"query": "test"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "failed"
        assert data["error"]["code"] == "API_ERROR"
        assert "boom" in data["error"]["message"]

    def test_get_report_not_found(self, analyze_client):
        mock_mem = MagicMock()
        mock_mem.get_report.return_value = None

        with patch("api.routes.analyze.get_long_term_memory", return_value=mock_mem):
            resp = analyze_client.get("/reports/missing-id")
        assert resp.status_code == 404

    def test_get_report_found(self, analyze_client):
        report_id = str(uuid.uuid4())
        mock_mem = MagicMock()
        mock_mem.get_report.return_value = {"id": report_id, "query": "test"}

        with patch("api.routes.analyze.get_long_term_memory", return_value=mock_mem):
            resp = analyze_client.get(f"/reports/{report_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == report_id

    def test_list_reports(self, analyze_client):
        mock_mem = MagicMock()
        mock_mem.list_reports.return_value = [{"id": "r1"}, {"id": "r2"}]

        with patch("api.routes.analyze.get_long_term_memory", return_value=mock_mem):
            resp = analyze_client.get("/reports?user_id=u1&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_clear_short_term_memory_all(self, analyze_client):
        mock_orch = MagicMock()
        mock_orch.clear_short_term_memory.return_value = 5

        with patch("api.routes.analyze._get_orchestrator", return_value=mock_orch):
            resp = analyze_client.delete("/memory/short-term")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "all"
        assert data["cleared_sessions"] == 5

    def test_clear_short_term_memory_session(self, analyze_client):
        mock_orch = MagicMock()
        mock_orch.clear_short_term_memory.return_value = 1

        with patch("api.routes.analyze._get_orchestrator", return_value=mock_orch):
            resp = analyze_client.delete("/memory/short-term?session_id=sess1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "session"
        assert data["session_id"] == "sess1"

    def test_clear_long_term_memory_no_user_no_all(self, analyze_client):
        """未提供 user_id 且 all=false 应返回 400。"""
        resp = analyze_client.delete("/memory/long-term")
        assert resp.status_code == 400

    def test_clear_long_term_memory_both_false(self, analyze_client):
        """delete_memories 和 delete_reports 同时为 false 应返回 400。"""
        resp = analyze_client.delete(
            "/memory/long-term?user_id=u1&delete_memories=false&delete_reports=false"
        )
        assert resp.status_code == 400

    def test_clear_long_term_memory_by_user(self, analyze_client):
        mock_mem = MagicMock()
        mock_mem.delete_user_data.return_value = {"memories": 3, "reports": 1}

        with patch("api.routes.analyze.get_long_term_memory", return_value=mock_mem):
            resp = analyze_client.delete("/memory/long-term?user_id=u1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "user"
        assert data["user_id"] == "u1"

    def test_clear_long_term_memory_all(self, analyze_client):
        mock_mem = MagicMock()
        mock_mem.delete_all.return_value = {"memories": 10, "reports": 5}

        with patch("api.routes.analyze.get_long_term_memory", return_value=mock_mem):
            resp = analyze_client.delete("/memory/long-term?all=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "all"
