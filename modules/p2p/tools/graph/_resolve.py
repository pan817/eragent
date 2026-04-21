"""Entity ID resolution helper for graph tools.

Neo4j Entity nodes use internal IDs (e.g., po_header_id="1") as entity_id,
but users/LLM often reference business identifiers (e.g., "PO-2024-0001",
"SUP-001"). This module provides a resolver that tries multiple matching
strategies to find the correct entity.
"""

from __future__ import annotations

from typing import Any

from core.logging_utils import get_logger

_logger = get_logger(__name__)

# Business property names per entity type that can serve as alternative IDs
_BUSINESS_ID_FIELDS: dict[str, list[str]] = {
    "PurchaseOrder": ["po_number"],
    "Supplier": ["vendor_id", "segment1"],
    "Invoice": ["invoice_num"],
    "Payment": ["check_number"],
    "Receipt": ["receipt_num"],
    "Material": ["segment1"],
    "Contract": ["contract_number"],
    "Auction": ["document_number"],
    "SupplierSite": ["vendor_site_code"],
    "POLine": ["po_number"],  # POLine might be referenced by parent PO number
}


def _extract_records(result: Any) -> list[dict[str, Any]]:
    """Extract dicts from Neo4j EagerResult or list."""
    if isinstance(result, list):
        return result
    if hasattr(result, "records"):
        return [dict(r) for r in result.records]
    return []


async def resolve_entity(
    client: Any,
    entity_type: str,
    entity_id: str,
) -> dict[str, Any] | None:
    """Resolve an entity by trying entity_id first, then business properties.

    Returns the matched entity's properties dict, or None if not found.
    """
    # Strategy 1: exact match on entity_id
    cypher = """
    MATCH (n:Entity {entity_type: $type, entity_id: $id})
    RETURN n.entity_id AS entity_id, n.entity_type AS entity_type, n.name AS name
    LIMIT 1
    """
    result = await client.execute_cypher(cypher, {"type": entity_type, "id": entity_id})
    records = _extract_records(result)
    if records:
        return records[0]

    # Strategy 2: match on business property fields
    biz_fields = _BUSINESS_ID_FIELDS.get(entity_type, [])
    for field in biz_fields:
        cypher = f"""
        MATCH (n:Entity {{entity_type: $type}})
        WHERE n.{field} = $id
        RETURN n.entity_id AS entity_id, n.entity_type AS entity_type, n.name AS name
        LIMIT 1
        """
        result = await client.execute_cypher(cypher, {"type": entity_type, "id": entity_id})
        records = _extract_records(result)
        if records:
            _logger.info(
                "entity resolved via %s: %s %s → entity_id=%s",
                field, entity_type, entity_id, records[0].get("entity_id"),
            )
            return records[0]

    # Strategy 3: fuzzy match on name field (format: "{entity_type} {entity_id}")
    cypher = """
    MATCH (n:Entity {entity_type: $type})
    WHERE n.name =~ $pattern
    RETURN n.entity_id AS entity_id, n.entity_type AS entity_type, n.name AS name
    LIMIT 1
    """
    pattern = f"(?i).*{entity_id}.*"
    result = await client.execute_cypher(cypher, {"type": entity_type, "pattern": pattern})
    records = _extract_records(result)
    if records:
        _logger.info(
            "entity resolved via name fuzzy: %s %s → entity_id=%s",
            entity_type, entity_id, records[0].get("entity_id"),
        )
        return records[0]

    # Strategy 4: extract numeric suffix and try as entity_id
    # e.g., "PO-1" → "1", "SUP-001" → "001" → try both
    import re
    nums = re.findall(r"\d+", entity_id)
    for num in nums:
        if num == entity_id:
            continue  # already tried in strategy 1
        cypher = """
        MATCH (n:Entity {entity_type: $type, entity_id: $id})
        RETURN n.entity_id AS entity_id, n.entity_type AS entity_type, n.name AS name
        LIMIT 1
        """
        result = await client.execute_cypher(cypher, {"type": entity_type, "id": num})
        records = _extract_records(result)
        if records:
            _logger.info(
                "entity resolved via numeric extract: %s %s → entity_id=%s",
                entity_type, entity_id, records[0].get("entity_id"),
            )
            return records[0]

    _logger.warning("entity not found: %s %s (tried entity_id, business fields, name, numeric)", entity_type, entity_id)
    return None


async def resolve_entity_id(
    client: Any,
    entity_type: str,
    entity_id: str,
) -> str:
    """Resolve to the actual entity_id, falling back to the input if not found."""
    resolved = await resolve_entity(client, entity_type, entity_id)
    if resolved:
        return resolved.get("entity_id", entity_id)
    return entity_id
