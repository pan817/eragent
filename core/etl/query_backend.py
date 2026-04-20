"""Dual-backend query abstraction (graphiti / postgresql / hybrid).

Provides a :class:`QueryBackend` protocol and three implementations so the
tool layer can transparently switch between graph and SQL queries based on
the ``graphiti_etl.query_backend`` setting.
"""

from __future__ import annotations

import asyncio
from core.logging_utils import get_logger
from typing import Any, Protocol, runtime_checkable

_logger = get_logger(__name__)


@runtime_checkable
class QueryBackend(Protocol):
    """Query backend protocol — implemented by PostgreSQL, Graphiti, and Hybrid."""

    async def query_purchase_orders(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def query_invoices(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def query_receipts(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def query_payments(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def query_suppliers(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def get_entity_timeline(
        self, entity_type: str, entity_id: str
    ) -> list[dict[str, Any]]: ...
    async def get_entity_relationships(
        self, entity_type: str, entity_id: str, depth: int = 2
    ) -> list[dict[str, Any]]: ...


# ── PostgreSQL backend ────────────────────────────────────────


class PostgreSQLBackend:
    """Delegates to the existing P2PRepository (SQL queries)."""

    def __init__(self, repository: Any) -> None:
        self._repo = repository

    async def query_purchase_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._repo.query_purchase_orders, **kwargs)

    async def query_invoices(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._repo.query_invoices, **kwargs)

    async def query_receipts(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._repo.query_receipts, **kwargs)

    async def query_payments(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._repo.query_payments, **kwargs)

    async def query_suppliers(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []  # P2PRepository has no dedicated supplier list query

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []  # PostgreSQL does not support semantic search

    async def get_entity_timeline(
        self, entity_type: str, entity_id: str
    ) -> list[dict[str, Any]]:
        return []  # no graph traversal capability

    async def get_entity_relationships(
        self, entity_type: str, entity_id: str, depth: int = 2
    ) -> list[dict[str, Any]]:
        return []  # no multi-hop traversal


# ── Graphiti backend ──────────────────────────────────────────


class GraphitiBackend:
    """Delegates to the GraphitiClient (graph queries)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def query_purchase_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        query_parts = ["purchase orders"]
        if po := kwargs.get("po_number"):
            query_parts.append(po)
        if vid := kwargs.get("vendor_id"):
            query_parts.append(f"supplier {vid}")
        return await self._client.search(" ".join(query_parts), num_results=20)

    async def query_invoices(self, **kwargs: Any) -> list[dict[str, Any]]:
        query_parts = ["invoices"]
        if inv := kwargs.get("invoice_num"):
            query_parts.append(inv)
        if vid := kwargs.get("vendor_id"):
            query_parts.append(f"supplier {vid}")
        return await self._client.search(" ".join(query_parts), num_results=20)

    async def query_receipts(self, **kwargs: Any) -> list[dict[str, Any]]:
        query_parts = ["receipts"]
        if po := kwargs.get("po_number"):
            query_parts.append(f"PO {po}")
        return await self._client.search(" ".join(query_parts), num_results=20)

    async def query_payments(self, **kwargs: Any) -> list[dict[str, Any]]:
        query_parts = ["payments"]
        if cn := kwargs.get("check_number"):
            query_parts.append(cn)
        return await self._client.search(" ".join(query_parts), num_results=20)

    async def query_suppliers(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._client.search("suppliers", num_results=20)

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._client.search(query, **kwargs)

    async def get_entity_timeline(
        self, entity_type: str, entity_id: str
    ) -> list[dict[str, Any]]:
        query = f"timeline of {entity_type} {entity_id}"
        return await self._client.search(query, num_results=15)

    async def get_entity_relationships(
        self, entity_type: str, entity_id: str, depth: int = 2
    ) -> list[dict[str, Any]]:
        return await self._client.get_relationships(entity_type, entity_id, depth=depth)


# ── Hybrid backend ────────────────────────────────────────────


class HybridBackend:
    """Graphiti-first with PostgreSQL fallback on failure or empty result."""

    def __init__(
        self, graphiti: GraphitiBackend, postgresql: PostgreSQLBackend
    ) -> None:
        self._graphiti = graphiti
        self._postgresql = postgresql

    async def query_purchase_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._try_graphiti_first(
            self._graphiti.query_purchase_orders,
            self._postgresql.query_purchase_orders,
            **kwargs,
        )

    async def query_invoices(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._try_graphiti_first(
            self._graphiti.query_invoices,
            self._postgresql.query_invoices,
            **kwargs,
        )

    async def query_receipts(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._try_graphiti_first(
            self._graphiti.query_receipts,
            self._postgresql.query_receipts,
            **kwargs,
        )

    async def query_payments(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._try_graphiti_first(
            self._graphiti.query_payments,
            self._postgresql.query_payments,
            **kwargs,
        )

    async def query_suppliers(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._try_graphiti_first(
            self._graphiti.query_suppliers,
            self._postgresql.query_suppliers,
            **kwargs,
        )

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._try_graphiti_first(
            self._graphiti.search,
            self._postgresql.search,
            query,
            **kwargs,
        )

    async def get_entity_timeline(
        self, entity_type: str, entity_id: str
    ) -> list[dict[str, Any]]:
        return await self._try_graphiti_first(
            self._graphiti.get_entity_timeline,
            self._postgresql.get_entity_timeline,
            entity_type,
            entity_id,
        )

    async def get_entity_relationships(
        self, entity_type: str, entity_id: str, depth: int = 2
    ) -> list[dict[str, Any]]:
        return await self._try_graphiti_first(
            self._graphiti.get_entity_relationships,
            self._postgresql.get_entity_relationships,
            entity_type,
            entity_id,
            depth=depth,
        )

    @staticmethod
    async def _try_graphiti_first(
        graphiti_fn: Any, postgresql_fn: Any, *args: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        try:
            result = await graphiti_fn(*args, **kwargs)
            if result:
                return result
        except Exception:
            _logger.warning(
                "Graphiti query failed, falling back to PostgreSQL",
                exc_info=True,
            )
        return await postgresql_fn(*args, **kwargs)


# ── Factory ───────────────────────────────────────────────────


def create_query_backend(
    query_backend_mode: str,
    repository: Any,
    graphiti_client: Any | None = None,
) -> QueryBackend:
    """Create the appropriate query backend based on configuration.

    Args:
        query_backend_mode: One of "graphiti", "postgresql", "hybrid".
        repository: P2PRepository instance.
        graphiti_client: GraphitiClient instance (may be None).

    Returns:
        A :class:`QueryBackend` implementation.
    """
    if query_backend_mode == "postgresql" or graphiti_client is None:
        if query_backend_mode == "graphiti" and graphiti_client is None:
            _logger.warning(
                "query_backend=graphiti but GraphitiClient is None — "
                "falling back to postgresql"
            )
        return PostgreSQLBackend(repository)

    if query_backend_mode == "graphiti":
        return GraphitiBackend(graphiti_client)

    if query_backend_mode == "hybrid":
        return HybridBackend(
            graphiti=GraphitiBackend(graphiti_client),
            postgresql=PostgreSQLBackend(repository),
        )

    raise ValueError(f"Unknown query_backend mode: {query_backend_mode}")
