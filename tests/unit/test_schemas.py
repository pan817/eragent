"""API Schema 模型单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.schemas.analysis import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    AnomalyDetail,
    AnomalyRecord,
    DocumentRef,
    ErrorInfo,
    KPIStatus,
    KPIValue,
    Severity,
    SupplierKPIReport,
)


class TestAnalysisRequest:
    """分析请求模型测试。"""

    def test_valid_request(self) -> None:
        """有效请求应通过校验。"""
        req = AnalysisRequest(query="分析三路匹配异常")
        assert req.query == "分析三路匹配异常"
        assert req.user_id == "default"
        assert req.session_id == ""
        assert req.time_range_days is None
        assert req.analysis_type is None

    def test_empty_query_rejected(self) -> None:
        """空查询应拒绝。"""
        with pytest.raises(ValidationError):
            AnalysisRequest(query="")

    def test_with_all_params(self) -> None:
        """所有参数传入时应正确赋值。"""
        req = AnalysisRequest(
            query="分析供应商绩效",
            user_id="user-001",
            session_id="sess-001",
            time_range_days=90,
            analysis_type=AnalysisType.SUPPLIER_PERFORMANCE,
        )
        assert req.time_range_days == 90
        assert req.analysis_type == AnalysisType.SUPPLIER_PERFORMANCE

    def test_time_range_validation(self) -> None:
        """时间范围超出 [1, 365] 应拒绝。"""
        with pytest.raises(ValidationError):
            AnalysisRequest(query="test", time_range_days=0)
        with pytest.raises(ValidationError):
            AnalysisRequest(query="test", time_range_days=400)


class TestAnomalyRecord:
    """异常记录模型测试。"""

    def test_creation(self) -> None:
        """应能正确创建异常记录。"""
        record = AnomalyRecord(
            anomaly_id="ANO-20260401-0001",
            anomaly_type="three_way_match_amount",
            severity=Severity.HIGH,
            rule_id="RULE_P2P_THREE_WAY_MATCH_AMOUNT",
            details=AnomalyDetail(field="invoice_amount", variance_pct=12.0, tolerance_pct=5.0),
            description="发票金额偏差 12%",
            recommended_action="核实发票",
        )
        assert record.severity == Severity.HIGH
        assert record.details.variance_pct == 12.0


class TestKPIValue:
    """KPI 值模型测试。"""

    def test_creation(self) -> None:
        """应能正确创建 KPI 值。"""
        kpi = KPIValue(value=96.5, benchmark=95.0, status=KPIStatus.GOOD)
        assert kpi.value == 96.5
        assert kpi.unit == "%"
        assert kpi.status == KPIStatus.GOOD


class TestAnalysisResult:
    """分析结果模型测试。"""

    def test_creation(self) -> None:
        """应能正确创建分析结果。"""
        result = AnalysisResult(
            report_id="rpt-001",
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.THREE_WAY_MATCH,
            query="测试查询",
            user_id="default",
            session_id="sess-001",
            time_range="最近 30 天",
        )
        assert result.status == AnalysisStatus.SUCCESS
        assert result.anomalies == []
        assert result.error is None

    def test_with_error(self) -> None:
        """包含错误信息时应正确创建。"""
        result = AnalysisResult(
            report_id="rpt-002",
            status=AnalysisStatus.FAILED,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query="测试",
            user_id="default",
            session_id="sess-002",
            time_range="",
            error=ErrorInfo(code="TEST_ERROR", message="测试错误"),
        )
        assert result.error is not None
        assert result.error.code == "TEST_ERROR"
