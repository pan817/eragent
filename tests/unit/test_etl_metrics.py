"""Tests for ETL metrics and Orchestrator graph context enrichment."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.etl.metrics import _Metrics, etl_metrics


class TestETLMetrics:
    @pytest.fixture(autouse=True)
    def _reset(self):
        etl_metrics.reset()
        yield
        etl_metrics.reset()

    def test_inc_sync_total(self):
        etl_metrics.inc_sync_total("full", "success")
        etl_metrics.inc_sync_total("full", "success")
        etl_metrics.inc_sync_total("incremental", "failure")
        snap = etl_metrics.snapshot()
        assert snap["etl_sync_total"]["full:success"] == 2
        assert snap["etl_sync_total"]["incremental:failure"] == 1

    def test_inc_rows_extracted(self):
        etl_metrics.inc_rows_extracted("purchasing", "PO_HEADERS_ALL", 100)
        etl_metrics.inc_rows_extracted("purchasing", "PO_HEADERS_ALL", 50)
        snap = etl_metrics.snapshot()
        assert snap["etl_rows_extracted"]["purchasing:PO_HEADERS_ALL"] == 150

    def test_inc_nodes_and_edges(self):
        etl_metrics.inc_nodes_loaded("Supplier", 10)
        etl_metrics.inc_edges_loaded("CREATES_PO", 20)
        snap = etl_metrics.snapshot()
        assert snap["etl_nodes_loaded"]["Supplier"] == 10
        assert snap["etl_edges_loaded"]["CREATES_PO"] == 20

    def test_inc_errors(self):
        etl_metrics.inc_errors("payables", "DatabaseError")
        snap = etl_metrics.snapshot()
        assert snap["etl_errors"]["payables:DatabaseError"] == 1

    def test_llm_counters(self):
        etl_metrics.inc_llm_calls(3)
        etl_metrics.inc_llm_tokens(1500)
        snap = etl_metrics.snapshot()
        assert snap["etl_llm_calls"] == 3
        assert snap["etl_llm_tokens"] == 1500

    def test_record_sync_duration(self):
        etl_metrics.record_sync_duration("full", 12.345)
        etl_metrics.record_sync_duration("incremental", 1.23)
        snap = etl_metrics.snapshot()
        durations = snap["etl_sync_durations_recent"]
        assert len(durations) == 2
        assert durations[0]["sync_type"] == "full"
        assert durations[0]["duration_seconds"] == 12.345

    def test_set_watermark_lag(self):
        etl_metrics.set_watermark_lag("PO_HEADERS_ALL", 600.0)
        snap = etl_metrics.snapshot()
        assert snap["etl_watermark_lag_seconds"]["PO_HEADERS_ALL"] == 600.0

    def test_reset_clears_all(self):
        etl_metrics.inc_sync_total("full", "success")
        etl_metrics.inc_llm_calls(1)
        etl_metrics.reset()
        snap = etl_metrics.snapshot()
        assert snap["etl_sync_total"] == {}
        assert snap["etl_llm_calls"] == 0

    def test_duration_history_capped_at_100(self):
        for i in range(120):
            etl_metrics.record_sync_duration("incremental", float(i))
        m = _Metrics()
        # Use internal state of the singleton
        assert len(etl_metrics._sync_durations) == 100

    def test_snapshot_is_serialisable(self):
        import json
        etl_metrics.inc_sync_total("full", "success")
        etl_metrics.record_sync_duration("full", 1.0)
        snap = etl_metrics.snapshot()
        json_str = json.dumps(snap)
        assert '"etl_sync_total"' in json_str


class TestAdminMetricsEndpoint:
    def test_metrics_route_exists(self):
        from api.routes.etl import router
        routes = [r.path for r in router.routes]
        assert "/admin/etl/metrics" in routes


def _make_mock_provider() -> MagicMock:
    from modules.p2p.provider import P2PModuleProvider

    real = P2PModuleProvider()
    provider = MagicMock()
    provider.get_entity_types.return_value = real.get_entity_types()
    provider.get_analysis_keywords.return_value = real.get_analysis_keywords()
    provider.get_analysis_type_descriptions.return_value = real.get_analysis_type_descriptions()
    provider.get_role_descriptions.return_value = real.get_role_descriptions()
    provider.get_lookup_rules.return_value = real.get_lookup_rules()
    provider.get_dag_templates.return_value = real.get_dag_templates()
    provider.get_generic_dag_templates.return_value = real.get_generic_dag_templates()
    provider.get_reference_patterns.return_value = real.get_reference_patterns()
    provider.get_tools.return_value = []
    provider.get_graphiti_client.return_value = None
    provider.is_query_backend_available.return_value = False
    return provider


class TestOrchestratorGraphContext:
    def test_enrich_returns_empty_without_client(self):
        """When no GraphitiClient is injected, returns empty string."""
        from core.orchestrator.orchestrator import Orchestrator

        # Create orchestrator with minimal config
        from config.settings import Settings
        settings = Settings(
            app_name="test", debug=True,
        )
        orch = Orchestrator(settings=settings, provider=_make_mock_provider())

        signal = MagicMock()
        signal.analysis_type = "three_way_match"
        params: dict = {"vendor_id": "SUP-001"}

        result = asyncio.get_event_loop().run_until_complete(
            orch._enrich_with_graph_context(signal, params)
        )
        assert result == ""

    def test_enrich_with_supplier(self):
        """When GraphitiClient is available, queries for supplier profile."""
        from core.orchestrator.orchestrator import Orchestrator
        from config.settings import Settings

        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=[{"fact": "supplier data"}])

        provider = _make_mock_provider()
        provider.get_graphiti_client.return_value = mock_client

        settings = Settings(app_name="test", debug=True)
        orch = Orchestrator(settings=settings, provider=provider)

        signal = MagicMock()
        signal.analysis_type = "three_way_match"
        params = {"vendor_id": "SUP-001"}

        result = asyncio.get_event_loop().run_until_complete(
            orch._enrich_with_graph_context(signal, params)
        )
        assert "供应商画像" in result
