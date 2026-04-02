"""P2P 业务规则引擎单元测试。"""

from __future__ import annotations

from typing import Any

import pytest

from config.settings import P2PSettings
from modules.p2p.rules.three_way_match import ThreeWayMatchChecker
from modules.p2p.rules.price_variance import PriceVarianceAnalyzer
from modules.p2p.rules.payment_compliance import PaymentComplianceChecker
from modules.p2p.rules.supplier_performance import SupplierPerformanceCalculator


# ============================================================
# ThreeWayMatchChecker
# ============================================================


class TestThreeWayMatchChecker:
    """三路匹配异常检测器测试。"""

    @pytest.fixture()
    def checker(self, p2p_settings: P2PSettings) -> ThreeWayMatchChecker:
        return ThreeWayMatchChecker(settings=p2p_settings)

    def test_no_anomaly(self, checker: ThreeWayMatchChecker) -> None:
        """PO/GR/Invoice 完全匹配时，不应产生异常。"""
        po = [{"po_number": "PO-001", "po_amount": 100000.0, "po_quantity": 500.0}]
        gr = [{"po_number": "PO-001", "gr_number": "GR-001", "gr_quantity": 500.0}]
        inv = [{"po_number": "PO-001", "invoice_number": "INV-001", "invoice_amount": 100000.0}]
        result = checker.check(po, gr, inv)
        # 完全匹配不应有超容差异常（可能有接近边界的 LOW 预警）
        high_or_medium = [r for r in result if r.severity.value in ("HIGH", "MEDIUM")]
        assert len(high_or_medium) == 0

    def test_amount_anomaly(
        self, checker: ThreeWayMatchChecker, mock_po_data: list[dict], mock_invoice_data: list[dict]
    ) -> None:
        """发票金额偏差超容差时应检测到异常（INV-001 偏差 12%）。"""
        gr = [{"po_number": p["po_number"], "gr_number": f"GR-{i}", "gr_quantity": p["po_quantity"]}
              for i, p in enumerate(mock_po_data, 1)]
        result = checker.check(mock_po_data, gr, mock_invoice_data)
        amount_anomalies = [r for r in result if r.anomaly_type == "three_way_match_amount"
                           and r.severity.value in ("HIGH", "MEDIUM")]
        assert len(amount_anomalies) >= 1
        # INV-001 偏差 12%，应被检测
        inv001 = [a for a in amount_anomalies if a.documents.invoice_number == "INV-001"]
        assert len(inv001) == 1

    def test_quantity_anomaly(
        self, checker: ThreeWayMatchChecker, mock_po_data: list[dict], mock_gr_data: list[dict]
    ) -> None:
        """收货数量偏差超容差时应检测到异常（GR-003 偏差 15%）。"""
        inv = [{"po_number": p["po_number"], "invoice_number": f"INV-{i}", "invoice_amount": p["po_amount"]}
               for i, p in enumerate(mock_po_data, 1)]
        result = checker.check(mock_po_data, mock_gr_data, inv)
        qty_anomalies = [r for r in result if r.anomaly_type == "three_way_match_quantity"
                        and r.severity.value in ("HIGH", "MEDIUM")]
        # GR-003 偏差 15%，应被检测
        gr003 = [a for a in qty_anomalies if a.documents.gr_number == "GR-003"]
        assert len(gr003) == 1

    def test_severity_high_by_amount(self, checker: ThreeWayMatchChecker) -> None:
        """金额超过 50 万时应标记为 HIGH。"""
        po = [{"po_number": "PO-X", "po_amount": 600000.0, "po_quantity": 100.0}]
        inv = [{"po_number": "PO-X", "invoice_number": "INV-X", "invoice_amount": 640000.0}]
        gr = [{"po_number": "PO-X", "gr_number": "GR-X", "gr_quantity": 100.0}]
        result = checker.check(po, gr, inv)
        high = [r for r in result if r.severity.value == "HIGH"]
        assert len(high) >= 1

    def test_severity_medium(self, checker: ThreeWayMatchChecker) -> None:
        """偏差在容差 1-2 倍之间应标记为 MEDIUM。"""
        po = [{"po_number": "PO-M", "po_amount": 10000.0, "po_quantity": 100.0}]
        # 偏差 7%（容差 5% 的 1.4 倍）
        inv = [{"po_number": "PO-M", "invoice_number": "INV-M", "invoice_amount": 10700.0}]
        gr = [{"po_number": "PO-M", "gr_number": "GR-M", "gr_quantity": 100.0}]
        result = checker.check(po, gr, inv)
        medium = [r for r in result if r.severity.value == "MEDIUM"]
        assert len(medium) >= 1

    def test_severity_low_boundary(self, checker: ThreeWayMatchChecker) -> None:
        """偏差接近容差边界（90%-100%）应标记为 LOW。"""
        po = [{"po_number": "PO-L", "po_amount": 10000.0, "po_quantity": 100.0}]
        # 偏差 4.6%（容差 5% 的 92%，在 90%-100% 范围内）
        inv = [{"po_number": "PO-L", "invoice_number": "INV-L", "invoice_amount": 10460.0}]
        gr = [{"po_number": "PO-L", "gr_number": "GR-L", "gr_quantity": 100.0}]
        result = checker.check(po, gr, inv)
        low = [r for r in result if r.severity.value == "LOW"]
        assert len(low) >= 1

    def test_supplier_tolerance(self, p2p_settings: P2PSettings) -> None:
        """供应商级别容差应覆盖默认容差。"""
        p2p_settings.three_way_match.supplier_tolerances = {"SUP-001": 15.0}
        checker = ThreeWayMatchChecker(settings=p2p_settings)
        po = [{"po_number": "PO-S", "po_amount": 10000.0, "po_quantity": 100.0,
               "supplier_id": "SUP-001"}]
        # 偏差 12%，默认容差 5% 会触发，但供应商容差 15% 不应触发
        inv = [{"po_number": "PO-S", "invoice_number": "INV-S", "invoice_amount": 11200.0}]
        gr = [{"po_number": "PO-S", "gr_number": "GR-S", "gr_quantity": 100.0}]
        result = checker.check(po, gr, inv)
        high_medium = [r for r in result if r.severity.value in ("HIGH", "MEDIUM")]
        assert len(high_medium) == 0


# ============================================================
# PriceVarianceAnalyzer
# ============================================================


class TestPriceVarianceAnalyzer:
    """采购价格差异分析器测试。"""

    @pytest.fixture()
    def analyzer(self, p2p_settings: P2PSettings) -> PriceVarianceAnalyzer:
        return PriceVarianceAnalyzer(settings=p2p_settings)

    def test_no_anomaly(self, analyzer: PriceVarianceAnalyzer) -> None:
        """价格无偏差时不应产生异常。"""
        lines = [{"po_number": "PO-001", "material_code": "MAT-001", "unit_price": 200.0, "line_number": "1"}]
        prices = {"MAT-001": 200.0}
        result = analyzer.analyze(lines, prices)
        assert len(result) == 0

    def test_price_variance_detected(self, analyzer: PriceVarianceAnalyzer) -> None:
        """价格偏差超容差时应检测到异常。"""
        lines = [{"po_number": "PO-001", "material_code": "MAT-001", "unit_price": 230.0,
                  "line_number": "1", "supplier_name": "供应商A", "material_name": "轴承"}]
        prices = {"MAT-001": 200.0}
        result = analyzer.analyze(lines, prices)
        assert len(result) == 1
        assert result[0].anomaly_type == "price_variance"
        assert result[0].details.variance_pct is not None
        assert result[0].details.variance_pct > 5.0

    def test_missing_contract_price(self, analyzer: PriceVarianceAnalyzer) -> None:
        """合同价不存在时应跳过。"""
        lines = [{"po_number": "PO-001", "material_code": "MAT-999", "unit_price": 230.0}]
        prices = {"MAT-001": 200.0}
        result = analyzer.analyze(lines, prices)
        assert len(result) == 0


# ============================================================
# PaymentComplianceChecker
# ============================================================


class TestPaymentComplianceChecker:
    """付款合规性检查器测试。"""

    @pytest.fixture()
    def checker(self, p2p_settings: P2PSettings) -> PaymentComplianceChecker:
        return PaymentComplianceChecker(settings=p2p_settings)

    def test_payment_overdue(self, checker: PaymentComplianceChecker) -> None:
        """逾期付款应被检测。"""
        payments = [{"payment_number": "PAY-001", "invoice_number": "INV-001",
                     "payment_date": "2026-04-11", "payment_amount": 112000.0}]
        invoices = [{"invoice_number": "INV-001", "po_number": "PO-001",
                     "due_date": "2026-04-01", "invoice_amount": 112000.0, "supplier_name": "A"}]
        result = checker.check(payments, invoices)
        overdue = [r for r in result if r.anomaly_type == "payment_overdue"]
        assert len(overdue) == 1
        assert "逾期" in overdue[0].description

    def test_payment_early(self, checker: PaymentComplianceChecker) -> None:
        """提前付款超过阈值应被检测。"""
        payments = [{"payment_number": "PAY-002", "invoice_number": "INV-002",
                     "payment_date": "2026-03-20", "payment_amount": 50000.0}]
        invoices = [{"invoice_number": "INV-002", "po_number": "PO-002",
                     "due_date": "2026-04-05", "invoice_amount": 50000.0, "supplier_name": "A"}]
        result = checker.check(payments, invoices)
        early = [r for r in result if r.anomaly_type == "payment_early"]
        assert len(early) == 1
        assert "提前" in early[0].description

    def test_payment_no_anomaly(self, checker: PaymentComplianceChecker) -> None:
        """正常付款不应产生异常。"""
        payments = [{"payment_number": "PAY-003", "invoice_number": "INV-003",
                     "payment_date": "2026-04-03", "payment_amount": 50000.0}]
        invoices = [{"invoice_number": "INV-003", "po_number": "PO-003",
                     "due_date": "2026-04-05", "invoice_amount": 50000.0, "supplier_name": "B"}]
        result = checker.check(payments, invoices)
        assert len(result) == 0

    def test_discount_abuse(self, checker: PaymentComplianceChecker) -> None:
        """折扣滥用应被检测（超过折扣截止日仍按折扣付款）。"""
        payments = [{"payment_number": "PAY-004", "invoice_number": "INV-004",
                     "payment_date": "2026-04-20", "payment_amount": 76000.0}]
        invoices = [{"invoice_number": "INV-004", "po_number": "PO-004",
                     "due_date": "2026-04-15", "discount_due_date": "2026-04-05",
                     "invoice_amount": 80000.0, "supplier_name": "B"}]
        result = checker.check(payments, invoices)
        abuse = [r for r in result if r.anomaly_type == "discount_abuse"]
        assert len(abuse) == 1
        assert abuse[0].severity.value == "HIGH"


# ============================================================
# SupplierPerformanceCalculator
# ============================================================


class TestSupplierPerformanceCalculator:
    """供应商绩效 KPI 计算器测试。"""

    @pytest.fixture()
    def calc(self, p2p_settings: P2PSettings) -> SupplierPerformanceCalculator:
        return SupplierPerformanceCalculator(settings=p2p_settings)

    def test_kpi_good(self, calc: SupplierPerformanceCalculator) -> None:
        """所有 KPI 达标时应评为 GOOD。"""
        po = [{"po_number": "PO-001", "po_quantity": 100.0, "po_amount": 10000.0,
               "required_date": "2026-04-01", "unit_price": 100.0, "contract_price": 100.0}]
        gr = [{"po_number": "PO-001", "gr_quantity": 100.0, "receipt_date": "2026-03-30",
               "quality_passed": True}]
        inv = [{"po_number": "PO-001", "invoice_amount": 10000.0}]
        report = calc.calculate("SUP-001", "测试供应商", po, gr, inv, "2026-Q1")
        assert report.supplier_id == "SUP-001"
        assert report.kpis["otif_rate"].status.value == "GOOD"
        assert report.kpis["quality_pass_rate"].status.value == "GOOD"

    def test_kpi_below_target(self, calc: SupplierPerformanceCalculator) -> None:
        """KPI 低于基准值时应评为 BELOW_TARGET 或 CRITICAL。"""
        po = [
            {"po_number": f"PO-{i}", "po_quantity": 100.0, "po_amount": 10000.0,
             "required_date": "2026-04-01", "unit_price": 100.0, "contract_price": 100.0}
            for i in range(10)
        ]
        # 只有 6 个准时足量交付（OTIF=60%，基准 95%）
        gr: list[dict[str, Any]] = []
        for i in range(10):
            if i < 6:
                gr.append({"po_number": f"PO-{i}", "gr_quantity": 100.0,
                          "receipt_date": "2026-03-30", "quality_passed": True})
            else:
                gr.append({"po_number": f"PO-{i}", "gr_quantity": 50.0,
                          "receipt_date": "2026-04-10", "quality_passed": False})
        inv = [{"po_number": f"PO-{i}", "invoice_amount": 10000.0} for i in range(10)]
        report = calc.calculate("SUP-002", "差供应商", po, gr, inv, "2026-Q1")
        otif = report.kpis["otif_rate"]
        assert otif.value < 95.0
        assert otif.status.value in ("BELOW_TARGET", "CRITICAL")

    def test_kpi_empty_data(self, calc: SupplierPerformanceCalculator) -> None:
        """空数据应返回 0 值的 KPI。"""
        report = calc.calculate("SUP-X", "空供应商", [], [], [], "2026-Q1")
        assert report.kpis["otif_rate"].value == 0.0
        assert report.kpis["quality_pass_rate"].value == 0.0
