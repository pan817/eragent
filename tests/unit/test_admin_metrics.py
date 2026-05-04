"""api/routes/admin_metrics.py unit tests.

Uses FastAPI TestClient + mock to isolate DB dependencies.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.admin_metrics import _parse_window, router

_ENGINE_PATH = "core.database.engine.get_engine"
_INDEXER_PATH = "core.memory.get_chat_indexer"


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(router)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ── _parse_window ─────────────────────────────────────────────


class TestParseWindow:
    def test_valid_7d(self) -> None:
        assert _parse_window("7d") == 7

    def test_valid_30d(self) -> None:
        assert _parse_window("30d") == 30

    def test_valid_1d(self) -> None:
        assert _parse_window("1d") == 1

    def test_clamps_to_365(self) -> None:
        assert _parse_window("999d") == 365

    def test_invalid_returns_default(self) -> None:
        assert _parse_window("abc") == 7

    def test_whitespace_stripped(self) -> None:
        assert _parse_window(" 14d ") == 14


# ── /admin/metrics/route-hit-rate ─────────────────────────────


class TestRouteHitRate:
    def test_returns_rows(self, client: TestClient) -> None:
        fake_row = {"route_level": "L1", "analysis_type": "three_way_match",
                     "execution_path": "dag", "count": 5, "avg_confidence": 0.92}
        mock_conn = MagicMock()
        mock_conn.execute.return_value.mappings.return_value.all.return_value = [fake_row]
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch(_ENGINE_PATH, return_value=mock_engine):
            resp = client.get("/admin/metrics/route-hit-rate?window=7d")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["route_level"] == "L1"

    def test_db_error_returns_empty(self, client: TestClient) -> None:
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = RuntimeError("db down")

        with patch(_ENGINE_PATH, return_value=mock_engine):
            resp = client.get("/admin/metrics/route-hit-rate")

        assert resp.status_code == 200
        assert resp.json() == []


# ── /admin/metrics/chat-index-status ──────────────────────────


class TestChatIndexStatus:
    def test_returns_counts(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = [
            MagicMock(scalar=MagicMock(return_value=100)),
            MagicMock(scalar=MagicMock(return_value=3)),
        ]
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        mock_indexer = MagicMock()
        mock_indexer._queue = [1, 2]

        with (
            patch(_ENGINE_PATH, return_value=mock_engine),
            patch(_INDEXER_PATH, return_value=mock_indexer),
        ):
            resp = client.get("/admin/metrics/chat-index-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_messages"] == 100
        assert data["dead_letter_count"] == 3
        assert data["indexer_queue_depth"] == 2

    def test_db_error_returns_defaults(self, client: TestClient) -> None:
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = RuntimeError("db down")

        with patch(_ENGINE_PATH, return_value=mock_engine):
            resp = client.get("/admin/metrics/chat-index-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_messages"] == 0

    def test_no_indexer(self, client: TestClient) -> None:
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = RuntimeError("db down")

        with (
            patch(_ENGINE_PATH, return_value=mock_engine),
            patch(_INDEXER_PATH, return_value=None),
        ):
            resp = client.get("/admin/metrics/chat-index-status")

        assert resp.status_code == 200
        assert resp.json()["indexer_queue_depth"] == 0


# ── /admin/metrics/chat-search-quality ────────────────────────


class TestChatSearchQuality:
    def test_aggregates_hits(self, client: TestClient) -> None:
        row1 = MagicMock(a_hits="2", b_hits="0")
        row2 = MagicMock(a_hits="0", b_hits="1")
        row3 = MagicMock(a_hits="0", b_hits="0")
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [row1, row2, row3]
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch(_ENGINE_PATH, return_value=mock_engine):
            resp = client.get("/admin/metrics/chat-search-quality?window=30d")

        assert resp.status_code == 200
        data = resp.json()
        assert data["window_days"] == 30
        assert data["exact_hits"] == 1
        assert data["semantic_hits"] == 1
        assert data["zero_hits"] == 1
        assert data["total_searches"] == 3

    def test_db_error_returns_defaults(self, client: TestClient) -> None:
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = RuntimeError("db down")

        with patch(_ENGINE_PATH, return_value=mock_engine):
            resp = client.get("/admin/metrics/chat-search-quality")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_searches"] == 0


# ── /admin/metrics/session-recap-coverage ─────────────────────


class TestSessionRecapCoverage:
    def test_computes_coverage(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = [
            MagicMock(scalar=MagicMock(return_value=10)),
            MagicMock(scalar=MagicMock(return_value=7)),
        ]
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch(_ENGINE_PATH, return_value=mock_engine):
            resp = client.get("/admin/metrics/session-recap-coverage")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_sessions"] == 10
        assert data["summarized_sessions"] == 7
        assert data["coverage_pct"] == 70.0

    def test_zero_sessions(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = [
            MagicMock(scalar=MagicMock(return_value=0)),
            MagicMock(scalar=MagicMock(return_value=0)),
        ]
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch(_ENGINE_PATH, return_value=mock_engine):
            resp = client.get("/admin/metrics/session-recap-coverage")

        assert resp.status_code == 200
        assert resp.json()["coverage_pct"] == 0.0

    def test_db_error_returns_defaults(self, client: TestClient) -> None:
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = RuntimeError("db down")

        with patch(_ENGINE_PATH, return_value=mock_engine):
            resp = client.get("/admin/metrics/session-recap-coverage")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_sessions"] == 0
