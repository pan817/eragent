"""ETL data models shared across extractors, transformers, and loaders.

All classes are plain dataclasses — no ORM dependency, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


# ── Transform I/O ─────────────────────────────────────────────


@dataclass
class GraphitiNode:
    """A node to be written into Graphiti."""

    entity_type: str
    entity_id: str
    properties: dict[str, Any]
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    last_updated: datetime | None = None


@dataclass
class GraphitiEdge:
    """An edge to be written into Graphiti."""

    edge_type: str
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    created_at: datetime | None = None
    properties: dict[str, Any] | None = None


@dataclass
class TextRecord:
    """A free-text field pending LLM extraction."""

    source_node_type: str
    source_node_id: str
    field_name: str
    text: str


@dataclass
class TransformResult:
    """Output of :class:`StructuredTransformer.transform`."""

    nodes: list[GraphitiNode] = field(default_factory=list)
    edges: list[GraphitiEdge] = field(default_factory=list)
    free_text_records: list[TextRecord] = field(default_factory=list)


# ── Load result ───────────────────────────────────────────────


@dataclass
class LoadResult:
    """Outcome of a loader batch write."""

    loaded: int = 0
    skipped: int = 0
    failed: int = 0


# ── Sync result ───────────────────────────────────────────────


@dataclass
class SyncResult:
    """Aggregate outcome of a full / incremental sync run."""

    sync_type: str = "incremental"
    total_rows: int = 0
    total_nodes: int = 0
    total_edges: int = 0
    duration_ms: float = 0.0
    failed_tables: list[str] = field(default_factory=list)
    succeeded_tables: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sync_type": self.sync_type,
            "total_rows": self.total_rows,
            "total_nodes": self.total_nodes,
            "total_edges": self.total_edges,
            "duration_ms": self.duration_ms,
            "failed_tables": self.failed_tables,
            "succeeded_tables": self.succeeded_tables,
        }


# ── Mapping DSL ───────────────────────────────────────────────


@dataclass
class EdgeMapping:
    """Declarative rule for producing an edge from an EBS row."""

    edge_type: str
    source: tuple[str, str]  # (node_type, id_field_in_row)
    target: tuple[str, str]  # (node_type, id_field_in_row)
    timestamp_field: str | None = None
    condition: Callable[[dict[str, Any]], bool] | None = None


@dataclass
class TableMapping:
    """Declarative rule for converting one EBS table into nodes + edges."""

    node_type: str
    id_field: str
    property_mapping: dict[str, str | tuple[str, type]]
    temporal: dict[str, str | None]
    edges: list[EdgeMapping] = field(default_factory=list)
    free_text_fields: list[str] = field(default_factory=list)
