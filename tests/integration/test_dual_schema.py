"""Dual-schema integration tests — verify both oracle_ebs and new_erp work end-to-end.

Covers:
- new_erp table seeding + NewERPRepository basic queries
- API lifespan boots correctly with each schema
- Both schemas produce identical output contract shapes
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.schemas.analysis import AnalysisResult, AnalysisStatus, AnalysisType
from core.database import get_session_factory, init_database
from core.database.engine import create_engine_from_dsn
from modules.p2p.mock_data.generator import MockDataGenerator
from modules.p2p.schemas.new_erp.models import (
    NewApInvoice,
    NewApPayment,
    NewApSupplier,
    NewPoHeader,
    NewPoLine,
    NewPoLineLocation,
    NewRcvTransaction,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _parse_date_opt(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _seed_new_erp(session: Session, seed: int = 0, count: int = 500) -> None:
    """Populate new_erp tables from MockDataGenerator (same data as EBS)."""
    gen = MockDataGenerator(seed=seed)
    raw = gen.generate_all(count=count)

    for s in raw["suppliers"]:
        session.add(NewApSupplier(
            vendor_id=s["vendor_id"],
            vendor_name=s["vendor_name"],
            supplier_site_id=s["supplier_site_id"],
            terms_id=s["terms_id"],
            enabled_flag=s["enabled_flag"],
            segment1=s.get("segment1"),
            vendor_type_lookup_code=s.get("vendor_type_lookup_code"),
            start_date_active=_parse_date_opt(s.get("start_date_active")),
            last_update_date=None,
        ))

    for h in raw["po_headers"]:
        session.add(NewPoHeader(
            po_header_id=h["po_header_id"],
            po_number=h["po_number"],
            vendor_id=h["vendor_id"],
            vendor_name=h["vendor_name"],
            status=h["status"],
            creation_date=_parse_date(h["creation_date"]),
            total_amount=h["total_amount"],
            currency=h["currency"],
        ))

    for ln in raw["po_lines"]:
        session.add(NewPoLine(
            po_line_id=ln["po_line_id"],
            po_header_id=ln["po_header_id"],
            po_number=ln["po_number"],
            line_num=ln["line_num"],
            item_id=ln["item_id"],
            item_description=ln["item_description"],
            quantity=ln["quantity"],
            unit_price=ln["unit_price"],
            amount=ln["amount"],
            category_id=ln["category_id"],
            standard_price=ln["standard_price"],
        ))

    for loc in raw["po_line_locations"]:
        session.add(NewPoLineLocation(
            line_location_id=loc["line_location_id"],
            po_line_id=loc["po_line_id"],
            po_number=loc["po_number"],
            promised_date=_parse_date(loc["promised_date"]),
            need_by_date=_parse_date(loc["need_by_date"]),
            quantity=loc["quantity"],
        ))

    for t in raw["rcv_transactions"]:
        session.add(NewRcvTransaction(
            transaction_id=t["transaction_id"],
            shipment_header_id=t["shipment_header_id"],
            po_number=t["po_number"],
            po_line_id=t["po_line_id"],
            transaction_type=t["transaction_type"],
            quantity=t["quantity"],
            accepted_quantity=t["accepted_quantity"],
            rejected_quantity=t["rejected_quantity"],
            transaction_date=_parse_date(t["transaction_date"]),
            vendor_id=t["vendor_id"],
        ))

    for inv in raw["invoices"]:
        session.add(NewApInvoice(
            invoice_id=inv["invoice_id"],
            invoice_num=inv["invoice_num"],
            po_number=inv["po_number"],
            vendor_id=inv["vendor_id"],
            vendor_name=inv["vendor_name"],
            invoice_amount=inv["invoice_amount"],
            invoice_date=_parse_date(inv["invoice_date"]),
            due_date=_parse_date(inv["due_date"]),
            discount_due_date=_parse_date_opt(inv.get("discount_due_date")),
            approval_status=inv["approval_status"],
            terms_id=inv.get("terms_id", "NET30"),
        ))

    for p in raw["payments"]:
        session.add(NewApPayment(
            check_id=p["check_id"],
            check_number=p["check_number"],
            invoice_num=p["invoice_num"],
            vendor_id=p["vendor_id"],
            amount=p["amount"],
            check_date=_parse_date(p["check_date"]),
            payment_method_code=p["payment_method_code"],
        ))

    session.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def dual_engine():
    """SQLite engine with both EBS and new_erp tables + data."""
    engine = create_engine_from_dsn(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_database(engine, seed=0, count=500)
    sf = get_session_factory(engine)
    with sf() as session:
        _seed_new_erp(session, seed=0, count=500)
    yield engine, sf
    engine.dispose()


@pytest.fixture()
def ebs_repo(dual_engine):
    from modules.p2p.schemas.oracle_ebs.repository import OracleEBSRepository
    _, sf = dual_engine
    return OracleEBSRepository(sf)


@pytest.fixture()
def new_erp_repo(dual_engine):
    from modules.p2p.schemas.new_erp.repository import NewERPRepository
    _, sf = dual_engine
    return NewERPRepository(sf)


@pytest.fixture(params=["oracle_ebs", "new_erp"])
def schema_client(request, dual_engine):
    """TestClient parametrized across both schemas — yields inside patch context."""
    engine, sf = dual_engine
    erp_schema = request.param

    from modules.p2p.schemas import SchemaRegistry
    from modules.p2p.tools._inject import set_repository

    reg = SchemaRegistry.get(erp_schema)
    set_repository(reg.repository_factory(sf))

    with (
        patch("api.main.get_settings") as mock_settings,
        patch("api.main.get_engine", return_value=engine),
        patch("api.main.create_tables"),
        patch("api.main.get_session_factory", return_value=sf),
    ):
        from config.settings import AsyncAnalysisSettings

        mock_cfg = MagicMock()
        mock_cfg.app_name = "ERP Agent Test"
        mock_cfg.app_version = "0.1.0-test"
        mock_cfg.erp_schema = erp_schema
        mock_cfg.postgresql = MagicMock()
        mock_cfg.async_analysis = AsyncAnalysisSettings()
        mock_settings.return_value = mock_cfg

        from api.main import app
        with TestClient(app) as tc:
            yield tc


# ---------------------------------------------------------------------------
# Repository query tests — both schemas return data with correct contract
# ---------------------------------------------------------------------------

_OUTPUT_CONTRACT = {
    "purchase_orders": [
        "po_number", "vendor_id", "vendor_name", "material_category",
        "po_amount", "po_quantity", "unit_price", "contract_price",
        "status", "creation_date", "required_date", "material_code",
        "material_name", "line_number",
    ],
    "receipts": [
        "receipt_id", "gr_number", "po_number", "vendor_id",
        "gr_quantity", "receipt_date", "quality_passed",
    ],
    "invoices": [
        "invoice_num", "po_number", "vendor_id", "vendor_name",
        "invoice_amount", "due_date", "discount_due_date",
        "discount_amount", "approval_status", "creation_date",
    ],
    "payments": [
        "check_id", "check_number", "invoice_num", "vendor_id",
        "amount", "check_date", "payment_method_code",
    ],
    "suppliers": [
        "vendor_id", "vendor_name", "segment1", "vendor_type",
        "terms_id", "enabled_flag",
    ],
}


class TestNewERPRepositoryQueries:
    """NewERPRepository returns data from new_* tables."""

    def test_query_purchase_orders(self, new_erp_repo) -> None:
        rows = new_erp_repo.query_purchase_orders(days=9999)
        assert len(rows) > 0
        for key in _OUTPUT_CONTRACT["purchase_orders"]:
            assert key in rows[0], f"missing key: {key}"

    def test_query_receipts(self, new_erp_repo) -> None:
        rows = new_erp_repo.query_receipts(days=9999)
        assert len(rows) > 0
        for key in _OUTPUT_CONTRACT["receipts"]:
            assert key in rows[0], f"missing key: {key}"

    def test_query_invoices(self, new_erp_repo) -> None:
        rows = new_erp_repo.query_invoices(days=9999)
        assert len(rows) > 0
        for key in _OUTPUT_CONTRACT["invoices"]:
            assert key in rows[0], f"missing key: {key}"

    def test_query_payments(self, new_erp_repo) -> None:
        rows = new_erp_repo.query_payments(days=9999)
        assert len(rows) > 0
        for key in _OUTPUT_CONTRACT["payments"]:
            assert key in rows[0], f"missing key: {key}"

    def test_query_suppliers(self, new_erp_repo) -> None:
        rows = new_erp_repo.query_suppliers()
        assert len(rows) > 0
        for key in _OUTPUT_CONTRACT["suppliers"]:
            assert key in rows[0], f"missing key: {key}"

    def test_get_contract_prices(self, new_erp_repo) -> None:
        prices = new_erp_repo.get_contract_prices()
        assert isinstance(prices, dict)
        assert len(prices) > 0

    def test_flattened_purchase_orders(self, new_erp_repo) -> None:
        rows = new_erp_repo.get_flattened_purchase_orders()
        assert len(rows) > 0

    def test_flattened_receipts(self, new_erp_repo) -> None:
        rows = new_erp_repo.get_flattened_receipts()
        assert len(rows) > 0

    def test_flattened_invoices(self, new_erp_repo) -> None:
        rows = new_erp_repo.get_flattened_invoices()
        assert len(rows) > 0

    def test_flattened_payments(self, new_erp_repo) -> None:
        rows = new_erp_repo.get_flattened_payments()
        assert len(rows) > 0


class TestNewERPAggregateAnalysis:
    """NewERPRepository aggregate methods return valid results."""

    def test_analyze_receipt_anomalies(self, new_erp_repo) -> None:
        result = new_erp_repo.analyze_receipt_anomalies(days=9999)
        assert isinstance(result, list)

    def test_detect_duplicate_invoices(self, new_erp_repo) -> None:
        result = new_erp_repo.detect_duplicate_invoices(days=9999)
        assert isinstance(result, list)

    def test_analyze_discount_utilization(self, new_erp_repo) -> None:
        result = new_erp_repo.analyze_discount_utilization(days=9999)
        assert isinstance(result, dict)
        assert "total_eligible" in result

    def test_analyze_vendor_concentration(self, new_erp_repo) -> None:
        result = new_erp_repo.analyze_vendor_concentration(days=9999)
        assert isinstance(result, dict)
        assert "grand_total_spend" in result
        assert "top_vendors" in result

    def test_calculate_po_cycle_time(self, new_erp_repo) -> None:
        result = new_erp_repo.calculate_po_cycle_time(days=9999)
        assert isinstance(result, dict)
        assert "total_orders" in result
        assert result["total_orders"] > 0


class TestOutputContractParity:
    """Both schemas produce identical output key sets for the same queries."""

    def test_purchase_orders_keys_match(self, ebs_repo, new_erp_repo) -> None:
        ebs = ebs_repo.query_purchase_orders(days=9999)
        new = new_erp_repo.query_purchase_orders(days=9999)
        assert set(ebs[0].keys()) == set(new[0].keys())

    def test_receipts_keys_match(self, ebs_repo, new_erp_repo) -> None:
        ebs = ebs_repo.query_receipts(days=9999)
        new = new_erp_repo.query_receipts(days=9999)
        assert set(ebs[0].keys()) == set(new[0].keys())

    def test_invoices_keys_match(self, ebs_repo, new_erp_repo) -> None:
        ebs = ebs_repo.query_invoices(days=9999)
        new = new_erp_repo.query_invoices(days=9999)
        assert set(ebs[0].keys()) == set(new[0].keys())

    def test_payments_keys_match(self, ebs_repo, new_erp_repo) -> None:
        ebs = ebs_repo.query_payments(days=9999)
        new = new_erp_repo.query_payments(days=9999)
        assert set(ebs[0].keys()) == set(new[0].keys())

    def test_suppliers_keys_match(self, ebs_repo, new_erp_repo) -> None:
        ebs = ebs_repo.query_suppliers()
        new = new_erp_repo.query_suppliers()
        assert set(ebs[0].keys()) == set(new[0].keys())

    def test_row_counts_match(self, ebs_repo, new_erp_repo) -> None:
        """Same seed → same row counts."""
        assert (
            len(ebs_repo.query_purchase_orders(days=9999))
            == len(new_erp_repo.query_purchase_orders(days=9999))
        )
        assert (
            len(ebs_repo.query_receipts(days=9999))
            == len(new_erp_repo.query_receipts(days=9999))
        )
        assert (
            len(ebs_repo.query_invoices(days=9999))
            == len(new_erp_repo.query_invoices(days=9999))
        )


# ---------------------------------------------------------------------------
# API tests — parametrized across both schemas
# ---------------------------------------------------------------------------

class TestAPIHealthBothSchemas:
    def test_health_ok(self, schema_client: TestClient) -> None:
        with patch("api.main.get_settings") as mock_s:
            mock_cfg = MagicMock()
            mock_cfg.app_name = "ERP Agent Test"
            mock_cfg.app_version = "0.1.0-test"
            mock_s.return_value = mock_cfg
            resp = schema_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestAPIAnalyzeBothSchemas:
    def test_analyze_success(self, schema_client: TestClient) -> None:
        mock_result = AnalysisResult(
            report_id="test-rpt-001",
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.THREE_WAY_MATCH,
            query="dual schema test",
            user_id="default",
            session_id="test-sess",
            time_range="30d",
            report_markdown="# Dual Schema Report",
        )
        with patch("api.routes.analyze._get_orchestrator") as mock_orch:
            orch_instance = MagicMock()
            orch_instance.analyze = AsyncMock(return_value=mock_result)
            mock_orch.return_value = orch_instance
            resp = schema_client.post(
                "/api/v1/ptp-agent/analyze",
                json={"query": "dual schema test"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    def test_analyze_invalid_request(self, schema_client: TestClient) -> None:
        resp = schema_client.post(
            "/api/v1/ptp-agent/analyze", json={"query": ""},
        )
        assert resp.status_code == 422
