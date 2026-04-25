"""Tests for graph query tools (modules/p2p/tools/graph/)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.p2p.tools._inject import set_graph_schema, set_graphiti_client
from modules.p2p.schemas.oracle_ebs.graph_schema import ORACLE_EBS_GRAPH_SCHEMA
from modules.p2p.tools.graph import (
    compare_entities,
    detect_graph_anomalies,
    find_competing_suppliers,
    find_contract_coverage,
    find_path_between,
    get_entity_detail,
    query_entity_relationships,
    query_entity_timeline,
    query_risk_impact,
    query_supplier_profile,
    search_knowledge_graph,
    trace_procurement_chain,
)


@pytest.fixture(autouse=True)
def _inject_mock_client():
    """Inject a mock GraphitiClient for all tests in this module."""
    mock_client = MagicMock()
    mock_client.search = AsyncMock(return_value=[
        {"entity_type": "Supplier", "entity_id": "SUP-001", "fact": "test fact"},
    ])
    mock_client.execute_cypher = AsyncMock(return_value=[
        {"entity": {"entity_type": "Supplier", "entity_id": "SUP-001"}, "connections": []},
    ])
    mock_client.is_connected = True
    set_graphiti_client(mock_client)
    set_graph_schema(ORACLE_EBS_GRAPH_SCHEMA)
    yield mock_client
    set_graphiti_client(None)  # type: ignore[arg-type]
    set_graph_schema(None)


class TestSearchKnowledgeGraph:
    def test_basic_search(self, _inject_mock_client):
        result = asyncio.get_event_loop().run_until_complete(
            search_knowledge_graph.ainvoke({"query": "supplier performance"})
        )
        data = json.loads(result)
        assert len(data) >= 1
        _inject_mock_client.search.assert_awaited_once()

    def test_with_entity_types(self, _inject_mock_client):
        asyncio.get_event_loop().run_until_complete(
            search_knowledge_graph.ainvoke({
                "query": "test", "entity_types": "Supplier,PurchaseOrder"
            })
        )
        call_args = _inject_mock_client.search.call_args
        assert "Supplier" in call_args[0][0]

    def test_dual_path_search(self, _inject_mock_client):
        """搜索应同时查询 episode 和 entity 节点，合并去重返回。"""
        # Episode search returns one result
        _inject_mock_client.search = AsyncMock(return_value=[
            {"source": "episode", "entity_id": "SUP-001", "fact": "supplier info"},
        ])
        # Entity node search returns two results (one overlapping)
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"entity_type": "Supplier", "entity_id": "SUP-001",
             "vendor_name": "Huawei", "vendor_id": "SUP-001",
             "po_number": None, "invoice_num": None, "valid_from": "2024-01-01"},
            {"entity_type": "PurchaseOrder", "entity_id": "PO-001",
             "vendor_name": None, "vendor_id": "SUP-001",
             "po_number": "PO-001", "invoice_num": None, "valid_from": "2024-01-10"},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            search_knowledge_graph.ainvoke({"query": "SUP-001"})
        )
        data = json.loads(result)
        # Entity nodes first, deduplicated: SUP-001 (entity) + PO-001 (entity) + episode fact (if different id)
        assert len(data) >= 2
        # Both search paths should have been called
        _inject_mock_client.search.assert_awaited_once()
        _inject_mock_client.execute_cypher.assert_awaited_once()

    def test_entity_search_fallback_on_episode_empty(self, _inject_mock_client):
        """Episode 搜索为空时，Entity 节点搜索仍应返回结果。"""
        _inject_mock_client.search = AsyncMock(return_value=[])
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"entity_type": "Supplier", "entity_id": "SUP-002",
             "vendor_name": "ZTE", "vendor_id": "SUP-002",
             "po_number": None, "invoice_num": None, "valid_from": "2024-02-01"},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            search_knowledge_graph.ainvoke({"query": "SUP-002"})
        )
        data = json.loads(result)
        assert len(data) >= 1
        assert data[0]["entity_id"] == "SUP-002"


class TestGetEntityDetail:
    def test_returns_entity_and_connections(self, _inject_mock_client):
        # resolve_entity_id calls execute_cypher first, then get_entity_detail calls it again
        _inject_mock_client.execute_cypher = AsyncMock(side_effect=[
            # 1st call: resolve_entity_id (exact match on entity_id)
            [{"entity_id": "SUP-001", "entity_type": "Supplier", "name": "Supplier SUP-001"}],
            # 2nd call: actual get_entity_detail query
            [{"entity": {"entity_type": "Supplier", "entity_id": "SUP-001"}, "connections": []}],
        ])
        result = asyncio.get_event_loop().run_until_complete(
            get_entity_detail.ainvoke({"entity_type": "Supplier", "entity_id": "SUP-001"})
        )
        assert _inject_mock_client.execute_cypher.await_count == 2
        data = json.loads(result)
        assert "entity" in data or "connections" in data


class TestQueryEntityTimeline:
    def test_timeline(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(side_effect=[
            # 1st call: resolve_entity_id
            [{"entity_id": "SUP-001", "entity_type": "Supplier", "name": "Supplier SUP-001"}],
            # 2nd call: actual timeline query
            [{"event_type": "PurchaseOrder", "event_id": "PO-001", "event_time": "2024-01-01", "relationship": "CREATES_PO"}],
        ])
        result = asyncio.get_event_loop().run_until_complete(
            query_entity_timeline.ainvoke({
                "entity_type": "Supplier", "entity_id": "SUP-001"
            })
        )
        data = json.loads(result)
        assert len(data) >= 1
        assert _inject_mock_client.execute_cypher.await_count == 2  # resolve + query


class TestQueryEntityRelationships:
    def test_relationships(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"nodes": [{"type": "Supplier", "id": "S1"}], "edges": [{"source": "S1", "target": "PO1", "type": "CREATES_PO"}]},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            query_entity_relationships.ainvoke({
                "entity_type": "Supplier", "entity_id": "SUP-001", "depth": 2
            })
        )
        data = json.loads(result)
        assert "nodes" in data or "edges" in data

    def test_depth_clamped(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[])
        asyncio.get_event_loop().run_until_complete(
            query_entity_relationships.ainvoke({
                "entity_type": "Supplier", "entity_id": "SUP-001", "depth": 10
            })
        )
        cypher = _inject_mock_client.execute_cypher.call_args[0][0]
        # depth should be clamped to 3
        assert "*1..3" in cypher


class TestFindPathBetween:
    def test_path_found(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"nodes": [{"type": "Supplier", "id": "S1"}, {"type": "Invoice", "id": "I1"}], "edges": ["SUBMITS_INVOICE"]},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            find_path_between.ainvoke({
                "from_type": "Supplier", "from_id": "S1",
                "to_type": "Invoice", "to_id": "I1",
            })
        )
        data = json.loads(result)
        assert "nodes" in data


class TestQuerySupplierProfile:
    def test_profile(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"supplier": {"vendor_id": "V001"}, "po_count": 5, "receipt_count": 3,
             "invoice_count": 4, "payment_count": 2, "auction_count": 1, "sites": []},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            query_supplier_profile.ainvoke({"vendor_id": "V001"})
        )
        data = json.loads(result)
        assert data.get("po_count") == 5


class TestCompareEntities:
    def test_compare(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"vendor_id": "V001", "vendor_name": "A", "po_count": 5, "receipt_count": 3, "invoice_count": 2, "auction_count": 0},
            {"vendor_id": "V002", "vendor_name": "B", "po_count": 3, "receipt_count": 2, "invoice_count": 1, "auction_count": 1},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            compare_entities.ainvoke({
                "entity_type": "Supplier", "entity_ids": "V001,V002"
            })
        )
        data = json.loads(result)
        assert len(data) == 2


class TestDetectGraphAnomalies:
    def test_all_scope(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"entity": "PO-001", "anomaly_type": "missing_receipt", "description": "test"},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            detect_graph_anomalies.ainvoke({"scope": "all", "time_range_days": 30})
        )
        data = json.loads(result)
        assert len(data) >= 1

    def test_specific_scope(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[])
        result = asyncio.get_event_loop().run_until_complete(
            detect_graph_anomalies.ainvoke({"scope": "orphan_payment"})
        )
        data = json.loads(result)
        assert isinstance(data, list)

    def test_invalid_scope(self, _inject_mock_client):
        result = asyncio.get_event_loop().run_until_complete(
            detect_graph_anomalies.ainvoke({"scope": "nonexistent"})
        )
        data = json.loads(result)
        assert "error" in data


class TestQueryRiskImpact:
    def test_supplier_risk(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"type": "PurchaseOrder", "id": "PO-001", "path_count": 3},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            query_risk_impact.ainvoke({"impact_type": "supplier_risk", "entity_id": "V001"})
        )
        data = json.loads(result)
        assert data["impact_type"] == "supplier_risk"
        assert len(data["affected"]) >= 1


class TestTraceProcurementChain:
    def test_trace_from_po(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"type": "POLine", "id": "L1", "time": "2024-01-02", "relationship": "CONTAINS_LINE", "distance": 1},
            {"type": "Receipt", "id": "R1", "time": "2024-01-05", "relationship": "RECEIVES_LINE", "distance": 2},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            trace_procurement_chain.ainvoke({"entity_type": "PurchaseOrder", "entity_id": "PO-001"})
        )
        data = json.loads(result)
        assert len(data) >= 1
        assert data[0]["type"] == "POLine"


class TestFindContractCoverage:
    def test_all_coverage(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"po_number": "PO-001", "vendor_id": "V001", "item": "Widget",
             "unit_price": 10.0, "material_code": "M1",
             "contract_number": "C001", "contract_price": 9.5, "coverage": "covered"},
            {"po_number": "PO-002", "vendor_id": "V002", "item": "Gadget",
             "unit_price": 20.0, "material_code": "M2",
             "contract_number": None, "contract_price": None, "coverage": "uncovered"},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            find_contract_coverage.ainvoke({"vendor_id": "", "material_id": ""})
        )
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["coverage"] == "covered"

    def test_coverage_by_vendor(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[])
        result = asyncio.get_event_loop().run_until_complete(
            find_contract_coverage.ainvoke({"vendor_id": "V001"})
        )
        data = json.loads(result)
        assert isinstance(data, list)


class TestFindCompetingSuppliers:
    def test_by_vendor(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"auction_id": "A001", "auction_title": "Office Supplies",
             "status": "CLOSED", "competitor_id": "V002",
             "competitor_name": "Competitor Inc", "bid_amount": 5000, "award_status": "AWARDED"},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            find_competing_suppliers.ainvoke({"vendor_id": "V001"})
        )
        data = json.loads(result)
        assert len(data) >= 1
        assert data[0]["competitor_name"] == "Competitor Inc"

    def test_by_auction(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"auction_title": "IT Hardware", "status": "OPEN",
             "vendor_id": "V001", "vendor_name": "Supplier A",
             "bid_amount": 10000, "award_status": None},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            find_competing_suppliers.ainvoke({"auction_id": "A001"})
        )
        data = json.loads(result)
        assert len(data) >= 1

    def test_no_params_returns_error(self, _inject_mock_client):
        result = asyncio.get_event_loop().run_until_complete(
            find_competing_suppliers.ainvoke({"vendor_id": "", "auction_id": ""})
        )
        data = json.loads(result)
        assert "error" in data


class TestQueryRiskImpactAdditional:
    def test_material_disruption(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"po_number": "PO-001", "item": "Widget", "quantity": 100,
             "backup_contract": None, "risk_level": "no_alternative"},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            query_risk_impact.ainvoke({"impact_type": "material_disruption", "entity_id": "M001"})
        )
        data = json.loads(result)
        assert data["impact_type"] == "material_disruption"

    def test_payment_chain(self, _inject_mock_client):
        _inject_mock_client.execute_cypher = AsyncMock(return_value=[
            {"invoice_num": "INV-002", "amount": 5000, "due_date": "2024-02-01", "remaining": 5000, "risk_type": "chain_risk"},
        ])
        result = asyncio.get_event_loop().run_until_complete(
            query_risk_impact.ainvoke({"impact_type": "payment_chain", "entity_id": "INV-001"})
        )
        data = json.loads(result)
        assert data["impact_type"] == "payment_chain"

    def test_invalid_type(self, _inject_mock_client):
        result = asyncio.get_event_loop().run_until_complete(
            query_risk_impact.ainvoke({"impact_type": "invalid", "entity_id": "X"})
        )
        data = json.loads(result)
        assert "error" in data


class TestGraphToolsWithoutClient:
    def test_raises_when_no_client(self):
        set_graphiti_client(None)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError):
            asyncio.get_event_loop().run_until_complete(
                get_entity_detail.ainvoke({"entity_type": "Supplier", "entity_id": "S1"})
            )
