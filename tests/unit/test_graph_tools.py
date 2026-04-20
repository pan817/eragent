"""Tests for graph query tools (modules/p2p/tools/graph.py)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.p2p.tools._inject import set_graphiti_client
from modules.p2p.tools.graph import (
    compare_entities,
    detect_graph_anomalies,
    query_entity_relationships,
    query_entity_timeline,
    query_supplier_profile,
    search_knowledge_graph,
)


@pytest.fixture(autouse=True)
def _inject_mock_client():
    """Inject a mock GraphitiClient for all tests in this module."""
    mock_client = MagicMock()
    mock_client.search = AsyncMock(return_value=[
        {"entity_type": "Supplier", "entity_id": "SUP-001", "fact": "test fact"},
    ])
    mock_client.get_entity = AsyncMock(return_value={
        "entity_type": "Supplier", "entity_id": "SUP-001",
    })
    mock_client.get_relationships = AsyncMock(return_value=[
        {"edge_type": "CREATES_PO", "source_id": "SUP-001", "target_id": "1"},
    ])
    set_graphiti_client(mock_client)
    yield mock_client
    set_graphiti_client(None)  # type: ignore[arg-type]


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
                "query": "test",
                "entity_types": "Supplier,PurchaseOrder",
            })
        )
        call_args = _inject_mock_client.search.call_args
        assert "Supplier,PurchaseOrder" in call_args[0][0]


class TestQueryEntityTimeline:
    def test_timeline(self, _inject_mock_client):
        result = asyncio.get_event_loop().run_until_complete(
            query_entity_timeline.ainvoke({
                "entity_type": "PurchaseOrder",
                "entity_id": "1",
            })
        )
        data = json.loads(result)
        assert isinstance(data, list)

    def test_timeline_no_related(self, _inject_mock_client):
        asyncio.get_event_loop().run_until_complete(
            query_entity_timeline.ainvoke({
                "entity_type": "PurchaseOrder",
                "entity_id": "1",
                "include_related": False,
            })
        )
        call_args = _inject_mock_client.search.call_args
        assert call_args[1]["num_results"] == 5  # depth=1 * 5


class TestQueryEntityRelationships:
    def test_relationships(self, _inject_mock_client):
        result = asyncio.get_event_loop().run_until_complete(
            query_entity_relationships.ainvoke({
                "entity_type": "Supplier",
                "entity_id": "SUP-001",
            })
        )
        data = json.loads(result)
        assert isinstance(data, list)
        _inject_mock_client.get_relationships.assert_awaited_once()

    def test_depth_clamped(self, _inject_mock_client):
        asyncio.get_event_loop().run_until_complete(
            query_entity_relationships.ainvoke({
                "entity_type": "Supplier",
                "entity_id": "SUP-001",
                "depth": 10,
            })
        )
        call_args = _inject_mock_client.get_relationships.call_args
        assert call_args[1]["depth"] == 3  # clamped


class TestQuerySupplierProfile:
    def test_profile(self, _inject_mock_client):
        result = asyncio.get_event_loop().run_until_complete(
            query_supplier_profile.ainvoke({"vendor_id": "SUP-001"})
        )
        data = json.loads(result)
        assert data["vendor_id"] == "SUP-001"
        assert "graph_facts" in data


class TestCompareEntities:
    def test_compare(self, _inject_mock_client):
        result = asyncio.get_event_loop().run_until_complete(
            compare_entities.ainvoke({
                "entity_type": "Supplier",
                "entity_ids": "SUP-001,SUP-002",
            })
        )
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["entity_id"] == "SUP-001"
        assert data[1]["entity_id"] == "SUP-002"


class TestDetectGraphAnomalies:
    def test_all_scope(self, _inject_mock_client):
        result = asyncio.get_event_loop().run_until_complete(
            detect_graph_anomalies.ainvoke({"scope": "all"})
        )
        data = json.loads(result)
        assert isinstance(data, list)
        assert _inject_mock_client.search.await_count == 4  # 4 scope queries

    def test_specific_scope(self, _inject_mock_client):
        result = asyncio.get_event_loop().run_until_complete(
            detect_graph_anomalies.ainvoke({"scope": "orphan_payments"})
        )
        data = json.loads(result)
        assert isinstance(data, list)
        assert _inject_mock_client.search.await_count == 1


class TestGraphToolsWithoutClient:
    def test_raises_when_no_client(self):
        set_graphiti_client(None)  # type: ignore[arg-type]
        # Reset the global to None by using internal access
        import modules.p2p.tools._inject as inj
        inj._graphiti_client = None

        with pytest.raises(RuntimeError, match="GraphitiClient"):
            asyncio.get_event_loop().run_until_complete(
                search_knowledge_graph.ainvoke({"query": "test"})
            )
