"""API 路由额外覆盖测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.schemas.analysis import AnalysisResult, AnalysisStatus, AnalysisType


@pytest.fixture()
def client() -> TestClient:
    """创建 FastAPI 测试客户端，使用 SQLite 内存库替代 PostgreSQL。"""
    from core.database.engine import create_engine_from_dsn
    from core.database import get_session_factory, init_database, P2PRepository
    from modules.p2p.tools import set_repository

    engine = create_engine_from_dsn("sqlite:///:memory:")
    init_database(engine, seed=0)
    session_factory = get_session_factory(engine)
    set_repository(P2PRepository(session_factory))

    with (
        patch("api.main.get_settings") as mock_settings,
        patch("api.main.get_engine", return_value=engine),
        patch("api.main.create_tables"),
        patch("api.main.get_session_factory", return_value=session_factory),
    ):
        mock_cfg = MagicMock()
        mock_cfg.app_name = "ERP Agent Test"
        mock_cfg.app_version = "0.1.0-test"
        mock_cfg.postgresql = MagicMock()
        mock_settings.return_value = mock_cfg
        from api.main import app
        with TestClient(app) as tc:
            yield tc

    engine.dispose()


class TestAnalyzeEndpointExtra:
    """分析端点额外测试。"""

    def test_analyze_orchestrator_exception(self, client: TestClient) -> None:
        """Orchestrator 异常时应返回 FAILED 结果。"""
        with patch("api.routes.analyze._get_orchestrator") as mock_orch:
            orch_instance = MagicMock()
            orch_instance.analyze = AsyncMock(side_effect=RuntimeError("orchestrator error"))
            mock_orch.return_value = orch_instance

            resp = client.post("/api/v1/analyze", json={"query": "分析异常"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "failed"
        assert "orchestrator error" in data["error"]["message"]

    def test_analyze_auto_session_id(self, client: TestClient) -> None:
        """未提供 session_id 时应自动生成。"""
        mock_result = AnalysisResult(
            report_id="rpt-001",
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query="测试",
            user_id="default",
            session_id="auto-generated",
            time_range="最近 30 天",
        )
        with patch("api.routes.analyze._get_orchestrator") as mock_orch:
            orch_instance = MagicMock()
            orch_instance.analyze = AsyncMock(return_value=mock_result)
            mock_orch.return_value = orch_instance

            resp = client.post("/api/v1/analyze", json={
                "query": "分析",
                "user_id": "test-user",
            })
        assert resp.status_code == 200


class TestReportEndpoints:
    """报告端点测试。"""

    def test_get_report_found(self, client: TestClient) -> None:
        """报告存在时应返回 200。"""
        mock_report = {
            "id": "rpt-001",
            "query": "分析",
            "analysis_type": "three_way_match",
        }
        with patch("api.routes.analyze._get_long_term_memory") as mock_ltm:
            mock_mem = MagicMock()
            mock_mem.get_report.return_value = mock_report
            mock_ltm.return_value = mock_mem

            resp = client.get("/api/v1/reports/rpt-001")
        assert resp.status_code == 200
        assert resp.json()["id"] == "rpt-001"

    def test_get_report_not_found(self, client: TestClient) -> None:
        """报告不存在时应返回 404。"""
        with patch("api.routes.analyze._get_long_term_memory") as mock_ltm:
            mock_mem = MagicMock()
            mock_mem.get_report.return_value = None
            mock_ltm.return_value = mock_mem

            resp = client.get("/api/v1/reports/nonexistent")
        assert resp.status_code == 404

    def test_list_reports(self, client: TestClient) -> None:
        """列出报告应返回列表。"""
        mock_reports = [
            {"id": "r1", "query": "q1"},
            {"id": "r2", "query": "q2"},
        ]
        with patch("api.routes.analyze._get_long_term_memory") as mock_ltm:
            mock_mem = MagicMock()
            mock_mem.list_reports.return_value = mock_reports
            mock_ltm.return_value = mock_mem

            resp = client.get("/api/v1/reports?user_id=user1&limit=10")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_list_reports_default_params(self, client: TestClient) -> None:
        """使用默认参数列出报告。"""
        with patch("api.routes.analyze._get_long_term_memory") as mock_ltm:
            mock_mem = MagicMock()
            mock_mem.list_reports.return_value = []
            mock_ltm.return_value = mock_mem

            resp = client.get("/api/v1/reports")
        assert resp.status_code == 200
        assert resp.json() == []
