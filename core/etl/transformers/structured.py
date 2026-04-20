"""Structured transformer — declarative EBS row → Graphiti node + edge conversion.

Pure function: no I/O, no side effects, easy to unit-test.
"""

from __future__ import annotations

from core.logging_utils import get_logger
from datetime import date, datetime
from typing import Any

from core.etl.models import (
    EdgeMapping,
    GraphitiEdge,
    GraphitiNode,
    TableMapping,
    TextRecord,
    TransformResult,
)

_logger = get_logger(__name__)


def _to_datetime(val: Any) -> datetime | None:
    """Coerce a date/datetime/str to datetime, or return None."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None
    return None


def _coerce(val: Any, target_type: type) -> Any:
    """Best-effort type coercion; return None on failure."""
    if val is None:
        return None
    try:
        return target_type(val)
    except (ValueError, TypeError):
        return None


class StructuredTransformer:
    """Convert batches of EBS row-dicts into Graphiti nodes and edges."""

    def __init__(self, registry: dict[str, TableMapping]) -> None:
        self._registry = registry

    def transform(
        self, table_name: str, rows: list[dict[str, Any]]
    ) -> TransformResult:
        """Transform one batch from *table_name*.

        Returns nodes, edges, and free-text records for optional LLM
        extraction.
        """
        mapping = self._registry.get(table_name)
        if mapping is None:
            _logger.warning("No mapping for table %s — skipping %d rows", table_name, len(rows))
            return TransformResult()

        nodes: list[GraphitiNode] = []
        edges: list[GraphitiEdge] = []
        texts: list[TextRecord] = []

        for row in rows:
            node = self._build_node(mapping, row)
            if node is not None:
                nodes.append(node)

            for em in mapping.edges:
                edge = self._build_edge(em, row)
                if edge is not None:
                    edges.append(edge)

            for field_name in mapping.free_text_fields:
                text_val = row.get(field_name)
                if text_val and str(text_val).strip():
                    texts.append(
                        TextRecord(
                            source_node_type=mapping.node_type,
                            source_node_id=str(row.get(mapping.id_field, "")),
                            field_name=field_name,
                            text=str(text_val).strip(),
                        )
                    )

        return TransformResult(nodes=nodes, edges=edges, free_text_records=texts)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _build_node(mapping: TableMapping, row: dict[str, Any]) -> GraphitiNode | None:
        entity_id = row.get(mapping.id_field)
        if entity_id is None:
            return None

        props: dict[str, Any] = {}
        for prop_name, source in mapping.property_mapping.items():
            if isinstance(source, tuple):
                field_name, target_type = source
                props[prop_name] = _coerce(row.get(field_name), target_type)
            else:
                props[prop_name] = row.get(source)

        temporal = mapping.temporal
        return GraphitiNode(
            entity_type=mapping.node_type,
            entity_id=str(entity_id),
            properties=props,
            valid_from=_to_datetime(row.get(temporal.get("valid_from", ""))),
            valid_to=_to_datetime(row.get(temporal.get("valid_to", ""))),
            last_updated=_to_datetime(row.get(temporal.get("last_updated", ""))),
        )

    @staticmethod
    def _build_edge(em: EdgeMapping, row: dict[str, Any]) -> GraphitiEdge | None:
        if em.condition is not None and not em.condition(row):
            return None

        source_type, source_field = em.source
        target_type, target_field = em.target

        source_id = row.get(source_field)
        target_id = row.get(target_field)

        if source_id is None or target_id is None:
            return None

        created_at = (
            _to_datetime(row.get(em.timestamp_field))
            if em.timestamp_field
            else None
        )

        return GraphitiEdge(
            edge_type=em.edge_type,
            source_type=source_type,
            source_id=str(source_id),
            target_type=target_type,
            target_id=str(target_id),
            created_at=created_at,
        )
