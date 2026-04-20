"""Tests for dual-backend query abstraction (core/etl/query_backend.py)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.etl.query_backend import (
    GraphitiBackend,
    HybridBackend,
    PostgreSQLBackend,
    create_query_backend,
)


class TestPostgreSQLBackend:
    def test_delegates_to_repository(self):
        repo = MagicMock()
        repo.query_purchase_orders.return_value = [{"po_number": "PO-001"}]
        backend = PostgreSQLBackend(repo)
        result = asyncio.get_event_loop().run_until_complete(
            backend.query_purchase_orders(po_number="PO-001")
        )
        assert result == [{"po_number": "PO-001"}]

    def test_search_returns_empty(self):
        backend = PostgreSQLBackend(MagicMock())
        result = asyncio.get_event_loop().run_until_complete(
            backend.search("test query")
        )
        assert result == []

    def test_timeline_returns_empty(self):
        backend = PostgreSQLBackend(MagicMock())
        result = asyncio.get_event_loop().run_until_complete(
            backend.get_entity_timeline("Supplier", "SUP-001")
        )
        assert result == []


class TestGraphitiBackend:
    def test_search_delegates_to_client(self):
        client = MagicMock()
        client.search = AsyncMock(return_value=[{"fact": "test"}])
        backend = GraphitiBackend(client)
        result = asyncio.get_event_loop().run_until_complete(
            backend.search("test query")
        )
        assert result == [{"fact": "test"}]
        client.search.assert_awaited_once()

    def test_query_po_constructs_query(self):
        client = MagicMock()
        client.search = AsyncMock(return_value=[])
        backend = GraphitiBackend(client)
        asyncio.get_event_loop().run_until_complete(
            backend.query_purchase_orders(po_number="PO-001", vendor_id="SUP-001")
        )
        call_args = client.search.call_args
        assert "PO-001" in call_args[0][0]
        assert "SUP-001" in call_args[0][0]

    def test_relationships_delegates(self):
        client = MagicMock()
        client.get_relationships = AsyncMock(return_value=[{"edge": "test"}])
        backend = GraphitiBackend(client)
        result = asyncio.get_event_loop().run_until_complete(
            backend.get_entity_relationships("Supplier", "SUP-001", depth=2)
        )
        assert result == [{"edge": "test"}]


class TestHybridBackend:
    def test_graphiti_first_success(self):
        graphiti = MagicMock(spec=GraphitiBackend)
        graphiti.search = AsyncMock(return_value=[{"source": "graphiti"}])
        pg = MagicMock(spec=PostgreSQLBackend)
        pg.search = AsyncMock(return_value=[{"source": "pg"}])

        hybrid = HybridBackend(graphiti, pg)
        result = asyncio.get_event_loop().run_until_complete(
            hybrid.search("test")
        )
        assert result == [{"source": "graphiti"}]
        pg.search.assert_not_awaited()

    def test_fallback_on_graphiti_failure(self):
        graphiti = MagicMock(spec=GraphitiBackend)
        graphiti.query_invoices = AsyncMock(side_effect=RuntimeError("Neo4j down"))
        pg = MagicMock(spec=PostgreSQLBackend)
        pg.query_invoices = AsyncMock(return_value=[{"source": "pg"}])

        hybrid = HybridBackend(graphiti, pg)
        result = asyncio.get_event_loop().run_until_complete(
            hybrid.query_invoices(invoice_num="INV-001")
        )
        assert result == [{"source": "pg"}]

    def test_fallback_on_empty_graphiti(self):
        graphiti = MagicMock(spec=GraphitiBackend)
        graphiti.query_payments = AsyncMock(return_value=[])
        pg = MagicMock(spec=PostgreSQLBackend)
        pg.query_payments = AsyncMock(return_value=[{"source": "pg"}])

        hybrid = HybridBackend(graphiti, pg)
        result = asyncio.get_event_loop().run_until_complete(
            hybrid.query_payments()
        )
        assert result == [{"source": "pg"}]


class TestCreateQueryBackend:
    def test_postgresql_mode(self):
        repo = MagicMock()
        backend = create_query_backend("postgresql", repo)
        assert isinstance(backend, PostgreSQLBackend)

    def test_graphiti_mode(self):
        repo = MagicMock()
        client = MagicMock()
        backend = create_query_backend("graphiti", repo, client)
        assert isinstance(backend, GraphitiBackend)

    def test_hybrid_mode(self):
        repo = MagicMock()
        client = MagicMock()
        backend = create_query_backend("hybrid", repo, client)
        assert isinstance(backend, HybridBackend)

    def test_graphiti_without_client_falls_back(self):
        repo = MagicMock()
        backend = create_query_backend("graphiti", repo, None)
        assert isinstance(backend, PostgreSQLBackend)

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            create_query_backend("invalid", MagicMock(), MagicMock())
