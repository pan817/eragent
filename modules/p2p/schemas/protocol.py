"""P2P data access protocol and graph schema contract.

Defines the cross-schema contracts that all ERP schema implementations must satisfy.
Tools and rule engines depend only on these abstractions, never on concrete ORM models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class P2PRepositoryProtocol(Protocol):
    """P2P data access protocol.

    All methods return unified dict structures (see design.md section 2.4).
    Callers need not be aware of underlying schema differences.
    """

    # ── Basic queries (existing 6 methods) ──

    def query_purchase_orders(
        self,
        vendor_id: str = "",
        status: str = "",
        days: int = 30,
        po_number: str = "",
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]: ...

    def query_receipts(
        self,
        po_number: str = "",
        vendor_id: str = "",
        days: int = 30,
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]: ...

    def query_invoices(
        self,
        po_number: str = "",
        vendor_id: str = "",
        status: str = "",
        invoice_num: str = "",
        days: int = 30,
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]: ...

    def query_payments(
        self,
        invoice_num: str = "",
        vendor_id: str = "",
        check_number: str = "",
        days: int = 30,
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]: ...

    def query_suppliers(
        self,
        vendor_id: str = "",
    ) -> list[dict[str, Any]]: ...

    def get_contract_prices(self) -> dict[str, float]: ...

    # ── Flattened methods (rule engine) ──

    def get_flattened_purchase_orders(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]: ...

    def get_flattened_receipts(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]: ...

    def get_flattened_invoices(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]: ...

    def get_flattened_payments(
        self,
        vendor_id: str = "",
        po_number: str = "",
    ) -> list[dict[str, Any]]: ...

    def invalidate_contract_price_cache(self) -> None: ...

    # ── Aggregate analysis (sunk from advanced.py) ──

    def analyze_receipt_anomalies(
        self,
        vendor_id: str = "",
        po_number: str = "",
        days: int = 30,
    ) -> list[dict[str, Any]]: ...

    def detect_duplicate_invoices(
        self,
        vendor_id: str = "",
        days: int = 30,
    ) -> list[dict[str, Any]]: ...

    def analyze_discount_utilization(
        self,
        vendor_id: str = "",
        days: int = 30,
    ) -> dict[str, Any]: ...

    def analyze_vendor_concentration(
        self,
        days: int = 30,
        top_n: int = 10,
    ) -> dict[str, Any]: ...

    def calculate_po_cycle_time(
        self,
        days: int = 30,
        vendor_id: str = "",
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GraphSchema:
    """Graph database schema descriptor.

    Maps business concepts (purchase_order) to concrete node/edge type names
    (PurchaseOrder / CREATES_PO) for Cypher queries and entity resolution.
    """

    # key: business concept identifier (fixed across schemas)
    # value: concrete node type name (schema-specific)
    node_types: dict[str, str] = field(default_factory=dict)

    # key: business relationship identifier (fixed across schemas)
    # value: concrete edge type name (schema-specific)
    edge_types: dict[str, str] = field(default_factory=dict)

    # key: concrete node type name (matches node_types values)
    # value: property names that serve as business identifiers
    business_id_fields: dict[str, list[str]] = field(default_factory=dict)

    def node(self, concept: str) -> str:
        """Get node type name by business concept. Raises KeyError if not found."""
        return self.node_types[concept]

    def edge(self, relationship: str) -> str:
        """Get edge type name by business relationship. Raises KeyError if not found."""
        return self.edge_types[relationship]

    def biz_id_fields(self, node_type: str) -> list[str]:
        """Get business ID field names for a node type."""
        return self.business_id_fields.get(node_type, [])
