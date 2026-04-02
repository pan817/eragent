"""意图解析模块单元测试。"""

from __future__ import annotations

from api.schemas.analysis import AnalysisType
from core.orchestrator.intent import IntentParser


class TestIntentParser:
    """IntentParser 测试。"""

    def setup_method(self) -> None:
        self.parser = IntentParser()

    def test_parse_three_way_match(self) -> None:
        """包含'三路匹配'关键词应解析为 THREE_WAY_MATCH。"""
        t, _ = self.parser.parse("分析最近三个月的三路匹配异常")
        assert t == AnalysisType.THREE_WAY_MATCH

    def test_parse_price_variance(self) -> None:
        """包含'价格差异'关键词应解析为 PRICE_VARIANCE。"""
        t, _ = self.parser.parse("检查价格差异情况")
        assert t == AnalysisType.PRICE_VARIANCE

    def test_parse_payment_compliance(self) -> None:
        """包含'付款'关键词应解析为 PAYMENT_COMPLIANCE。"""
        t, _ = self.parser.parse("分析付款逾期风险")
        assert t == AnalysisType.PAYMENT_COMPLIANCE

    def test_parse_supplier_performance(self) -> None:
        """包含'供应商'关键词应解析为 SUPPLIER_PERFORMANCE。"""
        t, _ = self.parser.parse("评估供应商绩效")
        assert t == AnalysisType.SUPPLIER_PERFORMANCE

    def test_parse_comprehensive(self) -> None:
        """无明确关键词或多类型命中应返回 COMPREHENSIVE。"""
        t, _ = self.parser.parse("给我一份完整的采购分析报告")
        assert t == AnalysisType.COMPREHENSIVE

    def test_parse_multiple_types(self) -> None:
        """同时包含多个类型关键词应返回 COMPREHENSIVE。"""
        t, _ = self.parser.parse("分析供应商的付款情况")
        assert t == AnalysisType.COMPREHENSIVE

    def test_extract_supplier_id(self) -> None:
        """应从查询中提取 supplier_id。"""
        _, params = self.parser.parse("查看 SUP-001 的采购订单")
        assert params.get("supplier_id") == "SUP-001"

    def test_extract_days(self) -> None:
        """应从查询中提取天数。"""
        _, params = self.parser.parse("分析最近60天的三路匹配异常")
        assert params.get("days") == 60

    def test_extract_po_number(self) -> None:
        """应从查询中提取 PO 号。"""
        _, params = self.parser.parse("检查 PO-2024-0001 的匹配情况")
        assert "po_number" in params
