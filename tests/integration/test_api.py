"""FastAPI 集成测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.schemas.analysis import AnalysisResult, AnalysisStatus, AnalysisType


@pytest.fixture()
def client() -> TestClient:
    """创建 FastAPI 测试客户端，mock 掉 get_settings 避免依赖配置文件。"""
    with patch("api.main.get_settings") as mock_settings:
        mock_cfg = MagicMock()
        mock_cfg.app_name = "ERP Agent Test"
        mock_cfg.app_version = "0.1.0-test"
        mock_settings.return_value = mock_cfg

        from api.main import app
        yield TestClient(app)


class TestHealthCheck:
    """健康检查端点测试。"""

    def test_health_ok(self, client: TestClient) -> None:
        """GET /health 应返回 status=ok。"""
        with patch("api.main.get_settings") as mock_settings:
            mock_cfg = MagicMock()
            mock_cfg.app_name = "ERP Agent Test"
            mock_cfg.app_version = "0.1.0-test"
            mock_settings.return_value = mock_cfg
            resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


class TestAnalyzeEndpoint:
    """分析端点测试。"""

    def test_analyze_success(self, client: TestClient) -> None:
        """POST /api/v1/analyze 应返回分析结果。"""
        mock_result = AnalysisResult(
            report_id="test-rpt-001",
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.THREE_WAY_MATCH,
            query="测试查询",
            user_id="default",
            session_id="test-sess",
            time_range="最近 30 天",
            report_markdown="# 测试报告",
        )
        with patch("api.routes.analyze._get_orchestrator") as mock_orch:
            orch_instance = MagicMock()
            orch_instance.analyze = AsyncMock(return_value=mock_result)
            mock_orch.return_value = orch_instance

            resp = client.post("/api/v1/analyze", json={"query": "分析三路匹配异常"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"

    def test_analyze_invalid_request(self, client: TestClient) -> None:
        """空 query 应返回 422。"""
        resp = client.post("/api/v1/analyze", json={"query": ""})
        assert resp.status_code == 422
