"""Tests for structured data source abstraction (core/etl/query_backend.py)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.etl.query_backend import (
    Neo4jStructuredBackend,
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

    def test_query_suppliers_with_method(self):
        repo = MagicMock()
        repo.query_suppliers.return_value = [{"vendor_id": "V001"}]
        backend = PostgreSQLBackend(repo)
        result = asyncio.get_event_loop().run_until_complete(
            backend.query_suppliers(vendor_id="V001")
        )
        assert result == [{"vendor_id": "V001"}]

    def test_query_suppliers_without_method(self):
        repo = MagicMock(spec=[])  # no methods
        backend = PostgreSQLBackend(repo)
        result = asyncio.get_event_loop().run_until_complete(
            backend.query_suppliers()
        )
        assert result == []


class TestNeo4jStructuredBackend:
    def test_query_po_uses_cypher_with_edge_traversal(self):
        client = MagicMock()
        client.execute_cypher = AsyncMock(return_value=[])
        backend = Neo4jStructuredBackend(client)
        asyncio.get_event_loop().run_until_complete(
            backend.query_purchase_orders(po_number="PO-001", vendor_id="SUP-001")
        )
        client.execute_cypher.assert_awaited_once()
        cypher = client.execute_cypher.call_args[0][0]
        # With vendor_id, query traverses from Supplier via CREATES_PO edge
        assert "Supplier" in cypher
        assert "CREATES_PO" in cypher
        assert "CONTAINS_LINE" in cypher

    def test_query_invoices_uses_cypher(self):
        client = MagicMock()
        client.execute_cypher = AsyncMock(return_value=[])
        backend = Neo4jStructuredBackend(client)
        asyncio.get_event_loop().run_until_complete(
            backend.query_invoices(invoice_num="INV-001")
        )
        client.execute_cypher.assert_awaited_once()
        cypher = client.execute_cypher.call_args[0][0]
        assert "Invoice" in cypher

    def test_query_suppliers_uses_cypher(self):
        client = MagicMock()
        client.execute_cypher = AsyncMock(return_value=[])
        backend = Neo4jStructuredBackend(client)
        asyncio.get_event_loop().run_until_complete(
            backend.query_suppliers(vendor_id="V001")
        )
        client.execute_cypher.assert_awaited_once()


class TestCreateQueryBackend:
    def test_postgresql_mode(self):
        repo = MagicMock()
        backend = create_query_backend("postgresql", repo)
        assert isinstance(backend, PostgreSQLBackend)

    def test_hybrid_mode(self):
        repo = MagicMock()
        client = MagicMock()
        backend = create_query_backend("hybrid", repo, client)
        assert isinstance(backend, Neo4jStructuredBackend)

    def test_hybrid_without_client_falls_back(self):
        repo = MagicMock()
        backend = create_query_backend("hybrid", repo, None)
        assert isinstance(backend, PostgreSQLBackend)

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            create_query_backend("invalid", MagicMock(), MagicMock())
