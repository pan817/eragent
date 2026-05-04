"""Structured data source abstraction (postgresql / hybrid).

Provides a :class:`QueryBackend` protocol and two implementations so the
structured tool layer can transparently switch between SQL and Neo4j queries
based on the ``graphiti_etl.query_backend`` setting.

Graph-specific tools (relationship traversal, anomaly detection, etc.) bypass
this abstraction and call GraphitiClient directly.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from core.logging_utils import get_logger
from typing import Any, Protocol, runtime_checkable

_logger = get_logger(__name__)


@runtime_checkable
class QueryBackend(Protocol):
    """Structured data source — supplies flat business data to 15 structured tools."""

    async def query_purchase_orders(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def query_invoices(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def query_receipts(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def query_payments(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def query_suppliers(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def get_contract_prices(self) -> dict[str, float]: ...


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
        if hasattr(self._repo, "query_suppliers"):
            return await asyncio.to_thread(self._repo.query_suppliers, **kwargs)
        return []

    async def get_contract_prices(self) -> dict[str, float]:
        return await asyncio.to_thread(self._repo.get_contract_prices)


# ── Neo4j Structured backend ──────────────────────────────────


class Neo4jStructuredBackend:
    """Queries Neo4j Entity nodes via Cypher with edge-enhanced fields.

    Returns identical dict structures to PostgreSQLBackend so rule engines
    work unchanged. In hybrid mode, queries leverage edge relationships to
    add extra fields (has_receipt, has_invoice, has_payment).
    """

    def __init__(self, client: Any) -> None:
        self._client = client  # GraphitiClient with execute_cypher

    # ── Common filter helpers ────────────────────────────────

    @staticmethod
    def _add_days_filter(
        wheres: list[str],
        params: dict[str, Any],
        kwargs: dict[str, Any],
        date_node_alias: str = "n",
    ) -> None:
        """Add ``valid_from >= cutoff`` WHERE clause when ``days > 0``."""
        days = kwargs.get("days", 0)
        if not isinstance(days, int):
            try:
                days = int(days)
            except (ValueError, TypeError):
                days = 0
        if days > 0:
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            wheres.append(
                f"toString({date_node_alias}.valid_from) >= $cutoff_date"
            )
            params["cutoff_date"] = cutoff

    @staticmethod
    def _add_status_filter(
        wheres: list[str],
        params: dict[str, Any],
        kwargs: dict[str, Any],
        status_field: str,
    ) -> None:
        """Add case-insensitive status filter when ``status`` is non-empty."""
        status = kwargs.get("status", "")
        if status:
            wheres.append(f"toLower(toString({status_field})) = toLower($status_val)")
            params["status_val"] = status

    @staticmethod
    def _order_and_limit(kwargs: dict[str, Any], order_field: str = "n.valid_from") -> str:
        """Build ``ORDER BY ... LIMIT ...`` fragment (no leading space).

        # TECH-DEBT(#9): amount_desc/amount_asc 静默降级为日期排序
        """
        order_map = {
            "date_desc": f"ORDER BY {order_field} DESC",
            "date_asc": f"ORDER BY {order_field} ASC",
        }
        order_by = kwargs.get("order_by", "")
        order_clause = order_map.get(order_by, f"ORDER BY {order_field} DESC")
        limit = kwargs.get("limit", 0)
        if not isinstance(limit, int):
            try:
                limit = int(limit)
            except (ValueError, TypeError):
                limit = 0
        limit_clause = f" LIMIT {limit}" if limit > 0 else ""
        return f"{order_clause}{limit_clause}"

    # ── Query methods ────────────────────────────────────────

    async def query_purchase_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        vid = kwargs.get("vendor_id", "")
        po_num = kwargs.get("po_number", "")

        if vid:
            match = "MATCH (s:Entity {entity_type:'Supplier', entity_id: $vendor_id})-[:RELATES_TO {name:'CREATES_PO'}]->(po:Entity)-[:RELATES_TO {name:'CONTAINS_LINE'}]->(line:Entity)"
            params["vendor_id"] = vid
        else:
            match = "MATCH (po:Entity {entity_type:'PurchaseOrder'})-[:RELATES_TO {name:'CONTAINS_LINE'}]->(line:Entity)"

        wheres: list[str] = []
        if po_num:
            wheres.append("po.po_number = $po_number")
            params["po_number"] = po_num
        self._add_days_filter(wheres, params, kwargs, date_node_alias="po")
        self._add_status_filter(wheres, params, kwargs, status_field="po.status")

        where_clause = f"WHERE {' AND '.join(wheres)}" if wheres else ""
        ol = self._order_and_limit(kwargs, order_field="po.valid_from")

        cypher = f"""
        {match}
        {where_clause}
        OPTIONAL MATCH (po)<-[:RELATES_TO {{name:'CREATES_PO'}}]-(sup:Entity {{entity_type:'Supplier'}})
        RETURN properties(po) AS po_props, properties(line) AS line_props,
               sup.entity_id AS vendor_id, sup.vendor_name AS vendor_name,
               EXISTS {{(line)<-[:RELATES_TO {{name:'RECEIVES_LINE'}}]-(:Entity)}} AS has_receipt,
               EXISTS {{(line)<-[:RELATES_TO {{name:'INVOICES_LINE'}}]-(:Entity)}} AS has_invoice,
               EXISTS {{(line)<-[:RELATES_TO {{name:'INVOICES_LINE'}}]-(:Entity)-[:RELATES_TO {{name:'BELONGS_TO_INVOICE'}}]->(:Entity)<-[:RELATES_TO {{name:'PAYS_INVOICE'}}]-(:Entity)}} AS has_payment
        {ol}
        """
        result = await self._client.execute_cypher(cypher, params)
        return [self._po_from_props(r) for r in self._extract_records(result)]

    async def query_invoices(self, **kwargs: Any) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        vid = kwargs.get("vendor_id", "")
        inv_num = kwargs.get("invoice_num", "")
        po_num = kwargs.get("po_number", "")

        if vid:
            match = "MATCH (s:Entity {entity_type:'Supplier', entity_id: $vendor_id})-[:RELATES_TO {name:'SUBMITS_INVOICE'}]->(n:Entity)"
            params["vendor_id"] = vid
        elif po_num:
            match = (
                "MATCH (po:Entity {entity_type:'PurchaseOrder', po_number: $po_number})"
                "-[:RELATES_TO {name:'CONTAINS_LINE'}]->(line:Entity)"
                "<-[:RELATES_TO {name:'INVOICES_LINE'}]-(il:Entity)"
                "-[:RELATES_TO {name:'BELONGS_TO_INVOICE'}]->(n:Entity)"
            )
            params["po_number"] = po_num
        else:
            match = "MATCH (n:Entity {entity_type: 'Invoice'})"

        wheres: list[str] = []
        if inv_num:
            wheres.append("n.invoice_num = $invoice_num")
            params["invoice_num"] = inv_num
        self._add_days_filter(wheres, params, kwargs)
        self._add_status_filter(wheres, params, kwargs, status_field="n.approval_status")

        where_clause = f"WHERE {' AND '.join(wheres)}" if wheres else ""
        ol = self._order_and_limit(kwargs)

        cypher = f"{match} {where_clause} WITH DISTINCT n {ol} RETURN properties(n) AS props"
        result = await self._client.execute_cypher(cypher, params)
        return [self._invoice_from_props(r) for r in self._extract_records(result)]

    async def query_receipts(self, **kwargs: Any) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        vid = kwargs.get("vendor_id", "")
        po_num = kwargs.get("po_number", "")

        if vid:
            match = (
                "MATCH (s:Entity {entity_type:'Supplier', entity_id: $vendor_id})"
                "-[:RELATES_TO {name:'CREATES_PO'}]->(po:Entity)"
                "-[:RELATES_TO {name:'CONTAINS_LINE'}]->(line:Entity)"
                "<-[:RELATES_TO {name:'RECEIVES_LINE'}]-(n:Entity)"
            )
            params["vendor_id"] = vid
            wheres: list[str] = []
            if po_num:
                wheres.append("po.po_number = $po_number")
                params["po_number"] = po_num
        elif po_num:
            match = (
                "MATCH (po:Entity {entity_type:'PurchaseOrder', po_number: $po_number})"
                "-[:RELATES_TO {name:'CONTAINS_LINE'}]->(line:Entity)"
                "<-[:RELATES_TO {name:'RECEIVES_LINE'}]-(n:Entity)"
            )
            params["po_number"] = po_num
            wheres = []
        else:
            match = "MATCH (n:Entity {entity_type: 'Receipt'})"
            wheres = []

        self._add_days_filter(wheres, params, kwargs)

        where_clause = f"WHERE {' AND '.join(wheres)}" if wheres else ""
        ol = self._order_and_limit(kwargs)

        cypher = f"{match} {where_clause} WITH DISTINCT n {ol} RETURN properties(n) AS props"
        result = await self._client.execute_cypher(cypher, params)
        return [self._receipt_from_props(r) for r in self._extract_records(result)]

    async def query_payments(self, **kwargs: Any) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        vid = kwargs.get("vendor_id", "")
        cn = kwargs.get("check_number", "")
        inv_num = kwargs.get("invoice_num", "")

        if vid:
            match = (
                "MATCH (s:Entity {entity_type:'Supplier', entity_id: $vendor_id})"
                "-[:RELATES_TO {name:'SUBMITS_INVOICE'}]->(inv:Entity)"
                "<-[:RELATES_TO {name:'PAYS_INVOICE'}]-(n:Entity)"
            )
            params["vendor_id"] = vid
        elif inv_num:
            match = (
                "MATCH (inv:Entity {entity_type:'Invoice', invoice_num: $invoice_num})"
                "<-[:RELATES_TO {name:'PAYS_INVOICE'}]-(n:Entity)"
            )
            params["invoice_num"] = inv_num
        else:
            match = "MATCH (n:Entity {entity_type: 'Payment'})"

        wheres: list[str] = []
        if cn:
            wheres.append("n.check_number = $check_number")
            params["check_number"] = cn
        self._add_days_filter(wheres, params, kwargs)

        where_clause = f"WHERE {' AND '.join(wheres)}" if wheres else ""
        ol = self._order_and_limit(kwargs)

        cypher = f"{match} {where_clause} WITH DISTINCT n {ol} RETURN properties(n) AS props"
        result = await self._client.execute_cypher(cypher, params)
        return [self._payment_from_props(r) for r in self._extract_records(result)]

    async def query_suppliers(self, **kwargs: Any) -> list[dict[str, Any]]:
        wheres = ["n.entity_type = 'Supplier'"]
        params: dict[str, Any] = {}
        if vid := kwargs.get("vendor_id"):
            wheres.append("n.vendor_id = $vendor_id")
            params["vendor_id"] = vid

        cypher = (
            "MATCH (n:Entity) WHERE " + " AND ".join(wheres) + " "
            "RETURN properties(n) AS props"
        )
        result = await self._client.execute_cypher(cypher, params)
        return [self._supplier_from_props(r) for r in self._extract_records(result)]

    async def get_contract_prices(self) -> dict[str, float]:
        cypher = (
            "MATCH (n:Entity) WHERE n.entity_type = 'POLine' "
            "RETURN DISTINCT n.entity_id AS item_id, n.standard_price AS price"
        )
        result = await self._client.execute_cypher(cypher)
        prices: dict[str, float] = {}
        for record in self._extract_records(result):
            iid = record.get("item_id") or record.get("n.entity_id")
            price = record.get("price") or record.get("n.standard_price")
            if iid and price:
                try:
                    prices[str(iid)] = float(price)
                except (ValueError, TypeError):
                    pass
        return prices

    # ── Record extraction helpers ─────────────────────────────

    @staticmethod
    def _extract_records(result: Any) -> list[dict[str, Any]]:
        """Extract dicts from Neo4j EagerResult or list."""
        if isinstance(result, list):
            return result
        if hasattr(result, "records"):
            return [dict(r) for r in result.records]
        return []

    @staticmethod
    def _safe_float(val: Any, default: float = 0.0) -> float:
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    def _po_from_props(self, record: dict[str, Any]) -> dict[str, Any]:
        po_p = record.get("po_props", record.get("props", record))
        line_p = record.get("line_props", {})
        p = {**po_p, **line_p}  # merge po + line properties
        # vendor_id/vendor_name come from Supplier node traversal (record-level, not in po_props)
        resolved_vendor_id = record.get("vendor_id") or p.get("vendor_id", p.get("entity_id", ""))
        resolved_vendor_name = record.get("vendor_name") or p.get("vendor_name", "")
        result = {
            "po_number": p.get("po_number", ""),
            "vendor_id": resolved_vendor_id,
            "vendor_name": resolved_vendor_name,
            "material_category": p.get("category_id", ""),
            "po_amount": self._safe_float(p.get("total_amount")),
            "po_quantity": self._safe_float(p.get("quantity")),
            "unit_price": self._safe_float(p.get("unit_price")),
            "contract_price": self._safe_float(p.get("standard_price")),
            "status": str(p.get("status", "")).lower(),
            "creation_date": str(p.get("valid_from", "")),
            "required_date": str(p.get("need_by_date", p.get("valid_from", ""))),
            "material_code": p.get("item_id", ""),
            "material_name": p.get("item_description", ""),
            "line_number": str(p.get("line_num", "1")),
        }
        # Edge-enhanced fields (hybrid mode bonus)
        if "has_receipt" in record:
            result["has_receipt"] = record["has_receipt"]
            result["has_invoice"] = record.get("has_invoice", False)
            result["has_payment"] = record.get("has_payment", False)
        return result

    def _invoice_from_props(self, record: dict[str, Any]) -> dict[str, Any]:
        p = record.get("props", record)
        return {
            "invoice_num": p.get("invoice_num", ""),
            "po_number": p.get("po_number", ""),
            "vendor_id": p.get("vendor_id", ""),
            "vendor_name": p.get("vendor_name", ""),
            "invoice_amount": self._safe_float(p.get("invoice_amount")),
            "due_date": str(p.get("due_date", "")),
            "discount_due_date": str(p.get("discount_due_date", "")),
            "discount_amount": self._safe_float(p.get("invoice_amount")) * 0.98,
            "approval_status": str(p.get("approval_status", "")).lower(),
            "creation_date": str(p.get("valid_from", "")),
        }

    def _receipt_from_props(self, record: dict[str, Any]) -> dict[str, Any]:
        p = record.get("props", record)
        return {
            "receipt_id": f"GR-{p.get('entity_id', '')}",
            "gr_number": f"GR-{p.get('entity_id', '')}",
            "po_number": p.get("po_number", ""),
            "vendor_id": p.get("vendor_id", ""),
            "gr_quantity": self._safe_float(p.get("quantity")),
            "receipt_date": str(p.get("valid_from", "")),
            "quality_passed": str(p.get("rejected_quantity", "0")) == "0",
        }

    def _payment_from_props(self, record: dict[str, Any]) -> dict[str, Any]:
        p = record.get("props", record)
        return {
            "check_id": p.get("entity_id", ""),
            "check_number": p.get("check_number", ""),
            "invoice_num": p.get("invoice_num", ""),
            "vendor_id": p.get("vendor_id", ""),
            "amount": self._safe_float(p.get("amount")),
            "check_date": str(p.get("valid_from", "")),
            "payment_method_code": str(p.get("payment_method_code", "")).lower(),
        }

    def _supplier_from_props(self, record: dict[str, Any]) -> dict[str, Any]:
        p = record.get("props", record)
        return {
            "vendor_id": p.get("entity_id", ""),
            "vendor_name": p.get("vendor_name", ""),
            "supplier_site_id": p.get("supplier_site_id", ""),
            "terms_id": p.get("terms_id", ""),
            "enabled_flag": p.get("enabled_flag", "Y"),
        }


# ── Factory ───────────────────────────────────────────────────


def create_query_backend(
    query_backend_mode: str,
    repository: Any,
    graphiti_client: Any | None = None,
) -> QueryBackend:
    """Create the appropriate query backend based on configuration.

    Args:
        query_backend_mode: One of "postgresql" or "hybrid".
        repository: P2PRepository instance.
        graphiti_client: GraphitiClient instance (required for hybrid mode).

    Returns:
        A :class:`QueryBackend` implementation.
    """
    # Normalize: "graphiti" is treated as "hybrid" (backward compat)
    if query_backend_mode == "graphiti":
        query_backend_mode = "hybrid"

    if query_backend_mode == "postgresql" or graphiti_client is None:
        if query_backend_mode == "hybrid" and graphiti_client is None:
            _logger.warning(
                "query_backend=hybrid but GraphitiClient is None — "
                "falling back to postgresql"
            )
        return PostgreSQLBackend(repository)

    if query_backend_mode == "hybrid":
        return Neo4jStructuredBackend(graphiti_client)

    raise ValueError(f"Unknown query_backend mode: {query_backend_mode}")
