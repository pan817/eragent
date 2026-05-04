"""P2P Agent 工具集单元测试。"""

from __future__ import annotations

import json

import pytest

from modules.p2p.tools import (
    analyze_discount_utilization,
    analyze_receipt_anomalies,
    analyze_vendor_concentration,
    calculate_po_cycle_time,
    calculate_spend_analysis,
    calculate_supplier_kpis,
    detect_duplicate_invoices,
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

    async def test_query_purchase_orders(self) -> None:
        result = await query_purchase_orders.ainvoke({"vendor_id": "", "status": "", "days": 0})
        data = json.loads(result)
        assert len(data) >= 1
        assert "po_number" in data[0]
        assert "creation_date" in data[0]
        assert "vendor_id" in data[0]

    async def test_query_receipts(self) -> None:
        result = await query_receipts.ainvoke({"po_number": "", "vendor_id": "", "days": 0})
        data = json.loads(result)
        assert len(data) >= 1
        assert "po_number" in data[0]
        assert "gr_number" in data[0]

    async def test_query_invoices(self) -> None:
        result = await query_invoices.ainvoke({"po_number": "", "vendor_id": "", "status": "", "days": 0})
        data = json.loads(result)
        assert len(data) >= 1
        assert "invoice_num" in data[0]

    async def test_query_payments(self) -> None:
        result = await query_payments.ainvoke({"invoice_num": "", "vendor_id": "", "days": 0})
        data = json.loads(result)
        assert len(data) >= 1
        assert "amount" in data[0]
        assert data[0]["amount"] > 0

    async def test_query_po_with_limit(self) -> None:
        result = await query_purchase_orders.ainvoke(
            {"vendor_id": "", "status": "", "days": 0, "limit": 3},
        )
        data = json.loads(result)
        assert len(data) == 3

    async def test_query_po_with_order_by_date_desc(self) -> None:
        result = await query_purchase_orders.ainvoke(
            {"vendor_id": "", "status": "", "days": 0, "limit": 5, "order_by": "date_desc"},
        )
        data = json.loads(result)
        assert len(data) == 5
        dates = [row["creation_date"] for row in data]
        assert dates == sorted(dates, reverse=True)

    async def test_query_po_with_order_by_amount_desc(self) -> None:
        result = await query_purchase_orders.ainvoke(
            {"vendor_id": "", "status": "", "days": 0, "limit": 3, "order_by": "amount_desc"},
        )
        data = json.loads(result)
        assert len(data) == 3
        amounts = [row["po_amount"] for row in data]
        assert amounts == sorted(amounts, reverse=True)


class TestAnalysisTools:
    """分析检查工具测试。"""

    async def test_run_three_way_match(self) -> None:
        """三路匹配工具应返回 JSON 结果。"""
        result = await run_three_way_match.ainvoke({"po_number": ""})
        data = json.loads(result)
        assert isinstance(data, (list, dict))

    async def test_run_payment_compliance(self) -> None:
        """付款合规工具应返回 JSON 结果。"""
        result = await run_payment_compliance_check.ainvoke({"vendor_id": "", "days": 30})
        data = json.loads(result)
        assert isinstance(data, (list, dict))

    async def test_run_price_variance(self) -> None:
        """价格差异工具应返回 JSON 结果。"""
        result = await run_price_variance_analysis.ainvoke({"vendor_id": "", "days": 30})
        data = json.loads(result)
        assert isinstance(data, (list, dict))

    async def test_calculate_supplier_kpis(self) -> None:
        """供应商 KPI 工具应返回 JSON 结果。"""
        result = await calculate_supplier_kpis.ainvoke({"vendor_id": "SUP-001", "period": "2024-Q1"})
        data = json.loads(result)
        assert isinstance(data, dict)


class TestNewAnalysisTools:
    """第一梯队新增分析工具测试。"""

    async def test_calculate_spend_analysis_by_category(self) -> None:
        result = await calculate_spend_analysis.ainvoke({"group_by": "category", "days": 365})
        data = json.loads(result)
        assert isinstance(data, list)
        if data:
            assert "group_key" in data[0]
            assert "total_amount" in data[0]

    async def test_calculate_spend_analysis_by_supplier(self) -> None:
        result = await calculate_spend_analysis.ainvoke({"group_by": "supplier", "days": 365})
        data = json.loads(result)
        assert isinstance(data, list)

    async def test_analyze_receipt_anomalies(self) -> None:
        result = await analyze_receipt_anomalies.ainvoke({"days": 365})
        data = json.loads(result)
        assert isinstance(data, list)
        # mock 数据中有拒收记录，应检测到异常
        if data:
            assert "issues" in data[0]
            assert "po_number" in data[0]

    async def test_analyze_receipt_anomalies_by_supplier(self) -> None:
        result = await analyze_receipt_anomalies.ainvoke({"vendor_id": "SUP-001", "days": 365})
        data = json.loads(result)
        assert isinstance(data, list)

    async def test_detect_duplicate_invoices(self) -> None:
        result = await detect_duplicate_invoices.ainvoke({"days": 365})
        data = json.loads(result)
        assert isinstance(data, list)

    async def test_detect_duplicate_invoices_by_supplier(self) -> None:
        result = await detect_duplicate_invoices.ainvoke({"vendor_id": "SUP-001", "days": 365})
        data = json.loads(result)
        assert isinstance(data, list)

    async def test_analyze_discount_utilization(self) -> None:
        result = await analyze_discount_utilization.ainvoke({"days": 365})
        data = json.loads(result)
        assert isinstance(data, dict)
        assert "total_eligible" in data
        assert "utilization_rate" in data

    async def test_analyze_discount_utilization_by_supplier(self) -> None:
        result = await analyze_discount_utilization.ainvoke({"vendor_id": "SUP-001", "days": 365})
        data = json.loads(result)
        assert isinstance(data, dict)

    async def test_calculate_po_cycle_time(self) -> None:
        result = await calculate_po_cycle_time.ainvoke({"days": 365})
        data = json.loads(result)
        assert isinstance(data, dict)
        assert "total_orders" in data
        assert "details" in data

    async def test_calculate_po_cycle_time_by_supplier(self) -> None:
        result = await calculate_po_cycle_time.ainvoke({"vendor_id": "SUP-001", "days": 365})
        data = json.loads(result)
        assert isinstance(data, dict)

    async def test_analyze_vendor_concentration(self) -> None:
        result = await analyze_vendor_concentration.ainvoke({"days": 365})
        data = json.loads(result)
        assert isinstance(data, dict)
        assert "grand_total_spend" in data
        assert "top_vendors" in data
        assert "single_source_categories" in data

    async def test_analyze_vendor_concentration_top_n(self) -> None:
        result = await analyze_vendor_concentration.ainvoke({"days": 365, "top_n": 3})
        data = json.loads(result)
        assert isinstance(data, dict)
        assert len(data["top_vendors"]) <= 3
