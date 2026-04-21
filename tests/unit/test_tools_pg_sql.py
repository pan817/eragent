"""PG 工具集 SQL 分支测试（postgresql 模式）。

验证 advanced.py 中 5 个聚合工具在 postgresql 模式下走 SQL 实现。
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from modules.p2p.tools import (
    analyze_discount_utilization,
    analyze_receipt_anomalies,
    analyze_vendor_concentration,
    calculate_po_cycle_time,
    calculate_spend_analysis,
    detect_duplicate_invoices,
    query_vendor_master,
    set_repository,
)


@pytest.fixture(autouse=True)
def _inject_repository(repository):
    """注入测试用 Repository。"""
    set_repository(repository)


@pytest.fixture(autouse=True)
def _force_postgresql_mode():
    """强制 postgresql 模式以测试 SQL 分支。"""
    with patch("modules.p2p.tools.pg.advanced._get_mode", return_value="postgresql"):
        yield


class TestAdvancedToolsSQLBranch:
    """postgresql 模式下 advanced 工具走 SQL 实现。"""

    async def test_analyze_receipt_anomalies_sql(self) -> None:
        result = await analyze_receipt_anomalies.ainvoke({"days": 365})
        data = json.loads(result)
        assert isinstance(data, list)

    async def test_analyze_receipt_anomalies_with_vendor_sql(self) -> None:
        result = await analyze_receipt_anomalies.ainvoke({"vendor_id": "SUP-001", "days": 365})
        data = json.loads(result)
        assert isinstance(data, list)

    async def test_analyze_receipt_anomalies_with_po_sql(self) -> None:
        result = await analyze_receipt_anomalies.ainvoke({"po_number": "PO-001", "days": 365})
        data = json.loads(result)
        assert isinstance(data, list)

    async def test_detect_duplicate_invoices_sql(self) -> None:
        result = await detect_duplicate_invoices.ainvoke({"days": 365})
        data = json.loads(result)
        assert isinstance(data, list)

    async def test_detect_duplicate_invoices_with_vendor_sql(self) -> None:
        result = await detect_duplicate_invoices.ainvoke({"vendor_id": "SUP-001", "days": 365})
        data = json.loads(result)
        assert isinstance(data, list)

    async def test_analyze_discount_utilization_sql(self) -> None:
        result = await analyze_discount_utilization.ainvoke({"days": 365})
        data = json.loads(result)
        assert isinstance(data, dict)
        assert "total_eligible" in data

    async def test_analyze_discount_utilization_with_vendor_sql(self) -> None:
        result = await analyze_discount_utilization.ainvoke({"vendor_id": "SUP-001", "days": 365})
        data = json.loads(result)
        assert isinstance(data, dict)

    async def test_analyze_vendor_concentration_sql(self) -> None:
        result = await analyze_vendor_concentration.ainvoke({"days": 365})
        data = json.loads(result)
        assert isinstance(data, dict)
        assert "grand_total_spend" in data
        assert "top_vendors" in data

    async def test_analyze_vendor_concentration_top_n_sql(self) -> None:
        result = await analyze_vendor_concentration.ainvoke({"days": 365, "top_n": 3})
        data = json.loads(result)
        assert isinstance(data, dict)
        assert len(data["top_vendors"]) <= 3

    async def test_calculate_po_cycle_time_sql(self) -> None:
        result = await calculate_po_cycle_time.ainvoke({"days": 365})
        data = json.loads(result)
        assert isinstance(data, dict)
        assert "total_orders" in data

    async def test_calculate_po_cycle_time_with_vendor_sql(self) -> None:
        result = await calculate_po_cycle_time.ainvoke({"vendor_id": "SUP-001", "days": 365})
        data = json.loads(result)
        assert isinstance(data, dict)


class TestSpendAnalysisSQLBranch:
    """postgresql 模式下 calculate_spend_analysis 走 SQL。"""

    async def test_spend_by_category_sql(self) -> None:
        with patch("modules.p2p.tools.pg.analysis._calculate_spend_analysis_impl") as mock_impl:
            # 直接测试 SQL 函数
            from modules.p2p.tools.pg.analysis import _spend_analysis_sql, _get_repository
            repo = _get_repository()
            result = _spend_analysis_sql(repo, "category", 365)
            assert isinstance(result, list)

    async def test_spend_by_supplier_sql(self) -> None:
        from modules.p2p.tools.pg.analysis import _spend_analysis_sql, _get_repository
        repo = _get_repository()
        result = _spend_analysis_sql(repo, "supplier", 365)
        assert isinstance(result, list)


class TestQueryVendorMaster:
    """供应商主数据查询。"""

    async def test_query_all_vendors(self) -> None:
        result = await query_vendor_master.ainvoke({"vendor_ids": ""})
        data = json.loads(result)
        assert isinstance(data, list)

    async def test_query_specific_vendor(self) -> None:
        result = await query_vendor_master.ainvoke({"vendor_ids": "SUP-001"})
        data = json.loads(result)
        assert isinstance(data, list)
