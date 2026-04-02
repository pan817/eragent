"""
端到端集成测试（真实 LLM）。

验证完整请求链路：HTTP Request → FastAPI → Orchestrator → IntentParser
→ P2PAgent → LLM (qwen3-max) → Tools（mock 业务数据）→ Rules → HTTP Response。

所有组件真实运行，包括 LLM 调用。需要配置有效的 LLM_API_KEY 环境变量。
"""

from __future__ import annotations

from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="module")
def real_settings() -> Settings:
    """从 config.yaml + .env 加载真实配置。"""
    # 清除 get_settings 缓存，确保重新加载
    from config.settings import get_settings
    get_settings.cache_clear()
    return Settings.from_yaml()


@pytest.fixture(scope="module")
def e2e_client(real_settings: Settings):
    """创建真实 LLM 的端到端测试客户端。

    所有组件真实运行：IntentParser、P2PAgent、LLM、Tools、Rules。
    scope=module 让整个测试模块共享同一客户端，避免重复初始化 Agent。
    """
    import api.routes.analyze as analyze_mod
    analyze_mod._orchestrator = None
    analyze_mod._long_term_memory = None

    import modules.p2p.tools as tools_mod
    tools_mod._MOCK_CACHE = None

    from unittest.mock import patch
    with patch("config.settings.get_settings", return_value=real_settings):
        from api.main import app
        with TestClient(app) as client:
            yield client


# ============================================================
# 端到端测试用例
# ============================================================


class TestE2EThreeWayMatch:
    """三路匹配端到端测试。"""

    def test_three_way_match_full_flow(self, e2e_client: TestClient) -> None:
        """自然语言请求 → 意图解析 → LLM 驱动工具调用 → 规则检查 → 返回报告。"""
        resp = e2e_client.post("/api/v1/analyze", json={
            "query": "请分析最近的三路匹配异常情况",
            "user_id": "e2e-tester",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["analysis_type"] == "three_way_match"
        assert data["user_id"] == "e2e-tester"
        assert data["session_id"]
        assert data["report_id"]
        assert data["time_range"] == "最近 30 天"
        assert data["duration_ms"] > 0
        # LLM 应生成非空 Markdown 报告
        assert len(data["report_markdown"]) > 50


class TestE2EPriceVariance:
    """价格差异分析端到端测试。"""

    def test_price_variance_full_flow(self, e2e_client: TestClient) -> None:
        """价格差异分析全链路：LLM 应调用价格分析工具并生成报告。"""
        resp = e2e_client.post("/api/v1/analyze", json={
            "query": "分析采购价格差异",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["analysis_type"] == "price_variance"
        assert len(data["report_markdown"]) > 50


class TestE2EPaymentCompliance:
    """付款合规检查端到端测试。"""

    def test_payment_compliance_full_flow(self, e2e_client: TestClient) -> None:
        """付款合规全链路：LLM 应检测逾期/提前付款并生成报告。"""
        resp = e2e_client.post("/api/v1/analyze", json={
            "query": "检查付款合规性，是否有逾期付款",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["analysis_type"] == "payment_compliance"
        assert len(data["report_markdown"]) > 50


class TestE2ESupplierPerformance:
    """供应商绩效评估端到端测试。"""

    def test_supplier_kpi_full_flow(self, e2e_client: TestClient) -> None:
        """指定供应商 → LLM 调用 KPI 计算工具 → 返回绩效报告。"""
        from modules.p2p.mock_data.generator import MockDataGenerator
        gen = MockDataGenerator(seed=0)
        raw = gen.generate_all()
        supplier_id = raw["po_headers"][0]["supplier_id"]

        resp = e2e_client.post("/api/v1/analyze", json={
            "query": f"评估供应商 {supplier_id} 的绩效 KPI",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["analysis_type"] == "supplier_performance"
        assert len(data["report_markdown"]) > 50


class TestE2EComprehensive:
    """综合分析端到端测试。"""

    def test_comprehensive_analysis(self, e2e_client: TestClient) -> None:
        """模糊查询 → 意图解析为 COMPREHENSIVE → LLM 自主选择多个工具 → 综合报告。"""
        resp = e2e_client.post("/api/v1/analyze", json={
            "query": "帮我全面分析一下最近的采购数据，看看有什么异常",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["analysis_type"] == "comprehensive"
        assert len(data["report_markdown"]) > 100


class TestE2ECustomTimeRange:
    """自定义时间范围端到端测试。"""

    def test_explicit_time_range(self, e2e_client: TestClient) -> None:
        """用户指定 time_range_days=90 → 结果中正确反映。"""
        resp = e2e_client.post("/api/v1/analyze", json={
            "query": "三路匹配检查",
            "time_range_days": 90,
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["time_range"] == "最近 90 天"

    def test_time_range_from_query_text(self, e2e_client: TestClient) -> None:
        """查询文本含 "最近60天" → IntentParser 提取 → 时间范围正确。"""
        resp = e2e_client.post("/api/v1/analyze", json={
            "query": "分析最近60天的三路匹配情况",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["time_range"] == "最近 60 天"


class TestE2EExplicitAnalysisType:
    """显式指定分析类型端到端测试。"""

    def test_explicit_type_overrides_intent(self, e2e_client: TestClient) -> None:
        """显式 analysis_type 优先于意图解析。"""
        resp = e2e_client.post("/api/v1/analyze", json={
            "query": "三路匹配相关的分析",
            "analysis_type": "payment_compliance",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["analysis_type"] == "payment_compliance"


class TestE2EValidation:
    """请求校验端到端测试。"""

    def test_empty_query_returns_422(self, e2e_client: TestClient) -> None:
        """空查询文本 → 422。"""
        resp = e2e_client.post("/api/v1/analyze", json={"query": ""})
        assert resp.status_code == 422

    def test_query_too_long_returns_422(self, e2e_client: TestClient) -> None:
        """超长查询文本 → 422。"""
        resp = e2e_client.post("/api/v1/analyze", json={"query": "x" * 2001})
        assert resp.status_code == 422

    def test_invalid_time_range_returns_422(self, e2e_client: TestClient) -> None:
        """非法时间范围 → 422。"""
        resp = e2e_client.post("/api/v1/analyze", json={
            "query": "分析",
            "time_range_days": 0,
        })
        assert resp.status_code == 422


class TestE2EHealthCheck:
    """健康检查端到端测试。"""

    def test_health_endpoint(self, e2e_client: TestClient) -> None:
        """GET /health → status=ok。"""
        resp = e2e_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
