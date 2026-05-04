"""工具参数组合 + 输出正确性测试。

直接调用 PG 工具的 ainvoke()，传入关键参数组合，验证返回数据的内容正确性。
"""

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
)


class TestQueryPurchaseOrders:

    async def test_no_filter_returns_all(self):
        data = json.loads(await query_purchase_orders.ainvoke({"days": 0}))
        assert len(data) >= 40

    async def test_vendor_filter(self, seed_data_summary):
        vid = seed_data_summary["supplier_ids"][0]
        data = json.loads(
            await query_purchase_orders.ainvoke({"vendor_id": vid, "days": 0}),
        )
        assert all(row["vendor_id"] == vid for row in data)
        assert len(data) >= 1

    async def test_limit_1_order_date_desc(self, seed_data_summary):
        data = json.loads(
            await query_purchase_orders.ainvoke(
                {"days": 0, "limit": 1, "order_by": "date_desc"},
            ),
        )
        assert len(data) == 1
        assert data[0]["po_number"] == seed_data_summary["latest_po"]

    async def test_limit_5_order_date_desc(self):
        data = json.loads(
            await query_purchase_orders.ainvoke(
                {"days": 0, "limit": 5, "order_by": "date_desc"},
            ),
        )
        assert len(data) == 5
        dates = [row["creation_date"] for row in data]
        assert dates == sorted(dates, reverse=True)

    async def test_order_amount_desc(self):
        data = json.loads(
            await query_purchase_orders.ainvoke(
                {"days": 0, "limit": 3, "order_by": "amount_desc"},
            ),
        )
        amounts = [row["po_amount"] for row in data]
        assert amounts == sorted(amounts, reverse=True)

    async def test_status_filter(self):
        data = json.loads(
            await query_purchase_orders.ainvoke({"status": "APPROVED", "days": 0}),
        )
        if data:
            assert all(row["status"].lower() == "approved" for row in data)

    async def test_required_fields_present(self):
        data = json.loads(
            await query_purchase_orders.ainvoke({"days": 0, "limit": 1}),
        )
        required = {
            "po_number", "vendor_id", "vendor_name", "po_amount",
            "unit_price", "status", "creation_date",
        }
        assert required.issubset(data[0].keys())

    async def test_po_number_exact(self):
        data = json.loads(
            await query_purchase_orders.ainvoke(
                {"po_number": "PO-2024-0001", "days": 0},
            ),
        )
        assert len(data) == 1
        assert data[0]["po_number"] == "PO-2024-0001"


class TestQueryReceipts:

    async def test_no_filter(self):
        data = json.loads(await query_receipts.ainvoke({"days": 0}))
        assert len(data) >= 1

    async def test_by_po_number(self, seed_data_summary):
        po = seed_data_summary["latest_po"]
        data = json.loads(
            await query_receipts.ainvoke({"po_number": po, "days": 0}),
        )
        assert all(row["po_number"] == po for row in data)


class TestQueryInvoices:

    async def test_no_filter(self):
        data = json.loads(await query_invoices.ainvoke({"days": 0}))
        assert len(data) >= 1

    async def test_limit_and_order(self):
        data = json.loads(
            await query_invoices.ainvoke(
                {"days": 0, "limit": 3, "order_by": "date_desc"},
            ),
        )
        assert len(data) == 3
        dates = [row["creation_date"] for row in data]
        assert dates == sorted(dates, reverse=True)


class TestQueryPayments:

    async def test_no_filter(self):
        data = json.loads(await query_payments.ainvoke({"days": 0}))
        assert len(data) >= 1
        assert "amount" in data[0]

    async def test_amount_desc_order(self):
        data = json.loads(
            await query_payments.ainvoke(
                {"days": 0, "limit": 3, "order_by": "amount_desc"},
            ),
        )
        amounts = [row["amount"] for row in data]
        assert amounts == sorted(amounts, reverse=True)


class TestThreeWayMatch:

    async def test_all_pos(self):
        data = json.loads(await run_three_way_match.ainvoke({"po_number": ""}))
        assert isinstance(data, list)
        assert len(data) >= 1

    async def test_result_has_anomaly_fields(self):
        data = json.loads(await run_three_way_match.ainvoke({"po_number": ""}))
        assert len(data) >= 1
        assert "anomaly_type" in data[0] or "severity" in data[0]


class TestPriceVariance:

    async def test_all_vendors(self):
        data = json.loads(
            await run_price_variance_analysis.ainvoke({"vendor_id": "", "days": 0}),
        )
        assert isinstance(data, list)

    async def test_single_vendor(self, seed_data_summary):
        vid = seed_data_summary["supplier_ids"][0]
        data = json.loads(
            await run_price_variance_analysis.ainvoke({"vendor_id": vid, "days": 0}),
        )
        assert isinstance(data, list)


class TestPaymentCompliance:

    async def test_returns_list(self):
        data = json.loads(
            await run_payment_compliance_check.ainvoke({"vendor_id": "", "days": 365}),
        )
        assert isinstance(data, list)

    async def test_contains_status_field_if_not_empty(self):
        data = json.loads(
            await run_payment_compliance_check.ainvoke({"vendor_id": "", "days": 365}),
        )
        if data:
            assert "status" in data[0] or "compliance_status" in data[0]


class TestSupplierKpis:

    async def test_known_vendor(self, seed_data_summary):
        vid = seed_data_summary["supplier_ids"][0]
        data = json.loads(
            await calculate_supplier_kpis.ainvoke({"vendor_id": vid, "period": ""}),
        )
        assert isinstance(data, dict)
        assert vid in json.dumps(data)


class TestSpendAnalysis:

    async def test_by_category(self):
        data = json.loads(
            await calculate_spend_analysis.ainvoke({"group_by": "category", "days": 0}),
        )
        assert isinstance(data, list)
        if data:
            assert "total_amount" in data[0]
            assert data[0]["total_amount"] > 0


class TestVendorConcentration:

    async def test_returns_structure(self):
        data = json.loads(
            await analyze_vendor_concentration.ainvoke({"days": 0}),
        )
        assert "grand_total_spend" in data
        assert "top_vendors" in data
        assert data["grand_total_spend"] > 0

    async def test_top_n_limits(self):
        data = json.loads(
            await analyze_vendor_concentration.ainvoke({"days": 0, "top_n": 2}),
        )
        assert len(data["top_vendors"]) <= 2


class TestReceiptAnomalies:

    async def test_returns_list(self):
        data = json.loads(
            await analyze_receipt_anomalies.ainvoke({"days": 0}),
        )
        assert isinstance(data, list)

    async def test_has_po_number_field(self):
        data = json.loads(
            await analyze_receipt_anomalies.ainvoke({"days": 0}),
        )
        if data:
            assert "po_number" in data[0]
            assert "issues" in data[0]


class TestPoCycleTime:

    async def test_returns_structure(self):
        data = json.loads(
            await calculate_po_cycle_time.ainvoke({"days": 0}),
        )
        assert isinstance(data, dict)
        assert "total_orders" in data
        assert "details" in data


class TestDuplicateInvoices:

    async def test_returns_list(self):
        data = json.loads(
            await detect_duplicate_invoices.ainvoke({"days": 0}),
        )
        assert isinstance(data, list)
