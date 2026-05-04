"""api/routes/etl.py unit tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.etl import router


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(router)
    return a


@pytest.fixture
def client_no_scheduler(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def client_with_scheduler(app: FastAPI) -> TestClient:
    scheduler = MagicMock()
    scheduler._state = AsyncMock()
    scheduler._state.get_all_states = AsyncMock(return_value=[{"table": "po_headers", "status": "ok"}])
    scheduler.trigger_manual_sync = AsyncMock(return_value={"status": "submitted"})
    scheduler.get_sync_status = MagicMock(return_value={"running": False})
    app.state.etl_scheduler = scheduler
    return TestClient(app)


class TestETLStatus:
    def test_no_scheduler_returns_503(self, client_no_scheduler: TestClient) -> None:
        resp = client_no_scheduler.get("/admin/etl/status")
        assert resp.status_code == 503

    def test_returns_states(self, client_with_scheduler: TestClient) -> None:
        resp = client_with_scheduler.get("/admin/etl/status")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestETLTrigger:
    def test_incremental_trigger(self, client_with_scheduler: TestClient) -> None:
        resp = client_with_scheduler.post(
            "/admin/etl/trigger", json={"sync_type": "incremental"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "submitted"

    def test_invalid_sync_type(self, client_with_scheduler: TestClient) -> None:
        resp = client_with_scheduler.post(
            "/admin/etl/trigger", json={"sync_type": "invalid"},
        )
        assert resp.status_code == 400

    def test_no_scheduler_returns_503(self, client_no_scheduler: TestClient) -> None:
        resp = client_no_scheduler.post(
            "/admin/etl/trigger", json={"sync_type": "full"},
        )
        assert resp.status_code == 503


class TestETLSyncStatus:
    def test_returns_status(self, client_with_scheduler: TestClient) -> None:
        resp = client_with_scheduler.get("/admin/etl/sync-status")
        assert resp.status_code == 200
        assert resp.json()["running"] is False


class TestETLMetrics:
    def test_returns_snapshot(self, client_with_scheduler: TestClient) -> None:
        with patch("core.etl.metrics.etl_metrics") as m:
            m.snapshot.return_value = {"total_syncs": 5}
            resp = client_with_scheduler.get("/admin/etl/metrics")
        assert resp.status_code == 200
        assert resp.json()["total_syncs"] == 5


class TestETLTraces:
    def test_returns_traces(self, client_with_scheduler: TestClient) -> None:
        with patch("core.etl.tracing.etl_tracer") as t:
            t.get_recent_traces.return_value = [{"trace_id": "t1"}]
            resp = client_with_scheduler.get("/admin/etl/traces")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
