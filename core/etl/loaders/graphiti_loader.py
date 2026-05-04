"""Graphiti loader — writes structured nodes via Cypher, free text via episodes.

Architecture:
- **Structured nodes**: Batch Cypher ``UNWIND`` + ``MERGE`` directly into Neo4j
  (no LLM, no embedding)
- **Edges**: Batch Cypher ``UNWIND`` + ``MERGE``
- **Free text**: ``graphiti.add_episode()`` for LLM entity extraction (optional)

This avoids the performance cost of routing structured data through Graphiti's
LLM pipeline while preserving LLM extraction for unstructured fields.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid as _uuid
from core.logging_utils import get_logger
from itertools import islice
from typing import Any

from core.etl.client import GraphitiClient, EpisodeData
from core.etl.models import GraphitiEdge, GraphitiNode, LoadResult, TextRecord

_logger = get_logger(__name__)

_NODE_BATCH_SIZE = 500


def _batched(iterable: list, n: int) -> list[list]:
    """Split *iterable* into chunks of at most *n* items."""
    it = iter(iterable)
    chunks: list[list] = []
    while True:
        chunk = list(islice(it, n))
        if not chunk:
            break
        chunks.append(chunk)
    return chunks


class GraphitiLoader:
    """Writes structured data via Cypher and free text via Graphiti episodes.

    Args:
        client: Initialised :class:`GraphitiClient`.
        episode_batch_size: Rows merged into one episode for free-text loading.
        max_concurrency: Max concurrent writes (Cypher or episode).
    """

    def __init__(
        self,
        client: GraphitiClient,
        episode_batch_size: int = 50,
        max_concurrency: int = 3,
    ) -> None:
        self._client = client
        self._episode_batch_size = max(1, episode_batch_size)
        self._max_concurrency = max(1, max_concurrency)

    # ------------------------------------------------------------------
    # Structured nodes → Batch Cypher UNWIND + MERGE (no LLM)
    # ------------------------------------------------------------------

    async def load(
        self,
        nodes: list[GraphitiNode],
        edges: list[GraphitiEdge],
    ) -> LoadResult:
        """Write structured nodes directly to Neo4j via batch Cypher UNWIND.

        No LLM or embedding calls — just direct graph writes.
        Nodes are batched into chunks and written via a single UNWIND
        statement per chunk, reducing N round-trips to ceil(N/batch_size).
        """
        if not nodes:
            return LoadResult()

        t0 = time.monotonic()
        loaded = 0
        failed = 0

        for chunk in _batched(nodes, _NODE_BATCH_SIZE):
            try:
                await self._merge_nodes_batch(chunk)
                loaded += len(chunk)
            except Exception:
                _logger.warning(
                    "Batch node write failed (%d nodes), falling back to individual",
                    len(chunk),
                    exc_info=True,
                )
                ok, fail = await self._merge_nodes_individually(chunk)
                loaded += ok
                failed += fail

        duration_ms = (time.monotonic() - t0) * 1000
        _logger.info(
            "Loader: %d/%d nodes written (batch UNWIND), %d failed, %.0fms",
            loaded, len(nodes), failed, duration_ms,
        )
        return LoadResult(loaded=loaded, failed=failed)

    async def _merge_nodes_batch(self, nodes: list[GraphitiNode]) -> None:
        """MERGE a batch of entity nodes into Neo4j via UNWIND."""
        node_params = [self._node_to_params(n) for n in nodes]
        cypher = (
            "UNWIND $nodes AS n "
            "MERGE (e:Entity {name: n.name, group_id: n.group_id}) "
            "SET e += n.props, "
            "    e.entity_type = n.entity_type, "
            "    e.entity_id = n.entity_id, "
            "    e.uuid = coalesce(e.uuid, n.uuid), "
            "    e.summary = n.summary, "
            "    e.created_at = coalesce(e.created_at, datetime()) "
            "RETURN count(e) AS cnt"
        )
        await self._client.execute_cypher(cypher, {"nodes": node_params})

    async def _merge_nodes_individually(
        self, nodes: list[GraphitiNode],
    ) -> tuple[int, int]:
        """Fallback: write nodes one by one if batch UNWIND fails."""
        sem = asyncio.Semaphore(self._max_concurrency)
        ok = 0
        fail = 0

        async def _write_one(node: GraphitiNode) -> bool:
            async with sem:
                try:
                    params = self._node_to_params(node)
                    cypher = (
                        "MERGE (e:Entity {name: $name, group_id: $group_id}) "
                        "SET e += $props, "
                        "    e.entity_type = $entity_type, "
                        "    e.entity_id = $entity_id, "
                        "    e.uuid = coalesce(e.uuid, $uuid), "
                        "    e.summary = $summary, "
                        "    e.created_at = coalesce(e.created_at, datetime()) "
                        "RETURN e.name AS name"
                    )
                    await self._client.execute_cypher(cypher, params)
                    return True
                except Exception:
                    _logger.warning(
                        "Failed to write node %s:%s",
                        node.entity_type, node.entity_id,
                        exc_info=True,
                    )
                    return False

        results = await asyncio.gather(*[_write_one(n) for n in nodes])
        ok = sum(1 for r in results if r)
        fail = len(nodes) - ok
        return ok, fail

    @staticmethod
    def _node_to_params(node: GraphitiNode) -> dict[str, Any]:
        """Convert a GraphitiNode to Cypher parameter dict."""
        props: dict[str, Any] = {}
        for k, v in node.properties.items():
            if v is not None:
                props[k] = str(v) if not isinstance(v, (int, float, bool)) else v
        if node.valid_from:
            props["valid_from"] = str(node.valid_from)
        if node.valid_to:
            props["valid_to"] = str(node.valid_to)
        if node.last_updated:
            props["last_updated"] = str(node.last_updated)

        entity_name = f"{node.entity_type} {node.entity_id}"
        summary_parts = [entity_name]
        for k, v in list(node.properties.items())[:3]:
            if v is not None:
                summary_parts.append(f"{k}={v}")

        return {
            "name": entity_name,
            "group_id": "",
            "uuid": str(_uuid.uuid4()),
            "summary": ", ".join(summary_parts),
            "props": props,
            "entity_type": node.entity_type,
            "entity_id": str(node.entity_id),
        }

    # ------------------------------------------------------------------
    # Edges → Cypher MERGE (no LLM)
    # ------------------------------------------------------------------

    async def create_edges(self, edges: list[GraphitiEdge]) -> int:
        """Create edges in Neo4j via batch Cypher UNWIND.

        Sends all edges in a single Cypher statement using UNWIND,
        reducing N round-trips to 1.
        """
        if not edges:
            return 0

        # Build edge parameter list
        edge_params = [
            {
                "source_name": f"{e.source_type} {e.source_id}",
                "target_name": f"{e.target_type} {e.target_id}",
                "edge_type": e.edge_type,
                "fact": (
                    f"{e.source_type} {e.source_id} "
                    f"{e.edge_type} "
                    f"{e.target_type} {e.target_id}"
                ),
            }
            for e in edges
        ]

        cypher = (
            "UNWIND $edges AS e "
            "MATCH (src:Entity {name: e.source_name}) "
            "MATCH (tgt:Entity {name: e.target_name}) "
            "MERGE (src)-[r:RELATES_TO {name: e.edge_type}]->(tgt) "
            "SET r.fact = e.fact, "
            "    r.created_at = datetime(), "
            "    r.valid_at = datetime() "
            "RETURN count(r) AS cnt"
        )

        try:
            await self._client.execute_cypher(cypher, {"edges": edge_params})
            _logger.info("Edges: %d/%d created via batch Cypher", len(edges), len(edges))
            return len(edges)
        except Exception:
            _logger.warning(
                "Batch edge creation failed (%d edges), falling back to individual",
                len(edges),
                exc_info=True,
            )
            return await self._create_edges_individually(edges)

    async def _create_edges_individually(self, edges: list[GraphitiEdge]) -> int:
        """Fallback: create edges one by one if batch fails."""
        sem = asyncio.Semaphore(self._max_concurrency)
        created = 0

        async def _create_one(edge: GraphitiEdge) -> bool:
            async with sem:
                try:
                    cypher = (
                        "MATCH (src:Entity {name: $source_name}) "
                        "MATCH (tgt:Entity {name: $target_name}) "
                        "MERGE (src)-[r:RELATES_TO {name: $edge_type}]->(tgt) "
                        "SET r.fact = $fact, "
                        "    r.created_at = datetime(), "
                        "    r.valid_at = datetime() "
                        "RETURN count(r) AS cnt"
                    )
                    params = {
                        "source_name": f"{edge.source_type} {edge.source_id}",
                        "target_name": f"{edge.target_type} {edge.target_id}",
                        "edge_type": edge.edge_type,
                        "fact": (
                            f"{edge.source_type} {edge.source_id} "
                            f"{edge.edge_type} "
                            f"{edge.target_type} {edge.target_id}"
                        ),
                    }
                    await self._client.execute_cypher(cypher, params)
                    return True
                except Exception:
                    _logger.warning(
                        "Failed to create edge %s: %s(%s)->%s(%s)",
                        edge.edge_type,
                        edge.source_type, edge.source_id,
                        edge.target_type, edge.target_id,
                        exc_info=True,
                    )
                    return False

        results = await asyncio.gather(*[_create_one(e) for e in edges])
        created = sum(1 for ok in results if ok)
        _logger.info("Edges: %d/%d created via individual Cypher (fallback)", created, len(edges))
        return created

    # ------------------------------------------------------------------
    # Free text → Graphiti add_episode (with LLM)
    # ------------------------------------------------------------------

    async def load_free_text(
        self, records: list[TextRecord]
    ) -> int:
        """Write free-text records via Graphiti add_episode (LLM extraction).

        Only called for fields like comments, descriptions that need
        LLM-based entity extraction. This is the ONLY path that invokes LLM.
        """
        if not records:
            return 0

        # Group by source node type, batch into episodes
        groups: dict[str, list[TextRecord]] = {}
        for r in records:
            groups.setdefault(r.source_node_type, []).append(r)

        sem = asyncio.Semaphore(self._max_concurrency)
        loaded = 0

        async def _write_episode(ep: EpisodeData) -> bool:
            async with sem:
                try:
                    await self._client.add_episode(ep)
                    return True
                except Exception:
                    _logger.warning(
                        "Failed to load free-text episode %s",
                        ep.name, exc_info=True,
                    )
                    return False

        episodes: list[EpisodeData] = []
        for node_type, type_records in groups.items():
            for chunk in _batched(type_records, self._episode_batch_size):
                body_parts = []
                for r in chunk:
                    body_parts.append(
                        f"{r.source_node_type} {r.source_node_id} "
                        f"[{r.field_name}]: {r.text}"
                    )
                ep = EpisodeData(
                    name=f"free_text:{node_type}:batch({len(chunk)})",
                    body="\n\n".join(body_parts),
                    source="etl",
                    source_description=f"EBS {node_type} free-text fields",
                )
                episodes.append(ep)

        results = await asyncio.gather(*[_write_episode(ep) for ep in episodes])
        loaded = sum(1 for ok in results if ok)

        _logger.info(
            "Free text: %d episodes (%d records) processed via LLM",
            loaded, len(records),
        )
        return loaded

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_nodes_to_episode(
        nodes: list[GraphitiNode],
        all_edges: list[GraphitiEdge],
    ) -> EpisodeData:
        """Build an episode from nodes (test/backward compat helper)."""
        entity_type = nodes[0].entity_type
        body_parts = []
        for node in nodes:
            body_parts.append(f"{node.entity_type} {node.entity_id}:")
            body_parts.append(
                json.dumps(node.properties, default=str, ensure_ascii=False)
            )
            body_parts.append("")
        return EpisodeData(
            name=f"{entity_type}:batch({len(nodes)})",
            body="\n".join(body_parts),
            source="etl",
            source_description=f"EBS {entity_type} batch",
        )

    @staticmethod
    def _node_to_episode(
        node: GraphitiNode, edges: list[GraphitiEdge]
    ) -> EpisodeData:
        return GraphitiLoader._merge_nodes_to_episode([node], edges)
