"""P2P Agent 工具集单元测试。"""

from __future__ import annotations

import json

import pytest

from modules.p2p.tools import (
    calculate_supplier_kpis,
    query_invoices,
    query_payments,
    query_purchase_orders,
    query_receipts,
    run_payment_compliance_check,
    run_price_variance_analysis,
    run_three_way_match,
    set_repository,
)


@pytest.fixture(autouse=True)
def _inject_repository(repository):
    """自动注入测试用 Repository（基于 SQLite 内存库）。"""
    set_repository(repository)


class TestQueryTools:
    """数据查询工具测试。"""

    def test_query_purchase_orders(self) -> None:
        """应返回有效的 JSON 字符串。"""
        result = query_purchase_orders.invoke({"supplier_id": "", "status": "", "days": 30})
        data = json.loads(result)
        assert isinstance(data, list)

    def test_query_receipts(self) -> None:
        """应返回有效的 JSON 字符串。"""
        result = query_receipts.invoke({"po_number": "", "supplier_id": "", "days": 30})
        data = json.loads(result)
        assert isinstance(data, list)

    def test_query_invoices(self) -> None:
        """应返回有效的 JSON 字符串。"""
        result = query_invoices.invoke({"po_number": "", "supplier_id": "", "status": "", "days": 30})
        data = json.loads(result)
        assert isinstance(data, list)

    def test_query_payments(self) -> None:
        """应返回有效的 JSON 字符串。"""
        result = query_payments.invoke({"invoice_number": "", "supplier_id": "", "days": 30})
        data = json.loads(result)
        assert isinstance(data, list)


class TestAnalysisTools:
    """分析检查工具测试。"""

    def test_run_three_way_match(self) -> None:
        """三路匹配工具应返回 JSON 结果。"""
        result = run_three_way_match.invoke({"po_number": ""})
        data = json.loads(result)
        assert isinstance(data, (list, dict))

    def test_run_payment_compliance(self) -> None:
        """付款合规工具应返回 JSON 结果。"""
        result = run_payment_compliance_check.invoke({"supplier_id": "", "days": 30})
        data = json.loads(result)
        assert isinstance(data, (list, dict))

    def test_run_price_variance(self) -> None:
        """价格差异工具应返回 JSON 结果。"""
        result = run_price_variance_analysis.invoke({"supplier_id": "", "days": 30})
        data = json.loads(result)
        assert isinstance(data, (list, dict))

    def test_calculate_supplier_kpis(self) -> None:
        """供应商 KPI 工具应返回 JSON 结果。"""
        result = calculate_supplier_kpis.invoke({"supplier_id": "SUP-001", "period": "2024-Q1"})
        data = json.loads(result)
        assert isinstance(data, dict)
