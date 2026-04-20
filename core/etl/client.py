"""Graphiti client wrapper.

Encapsulates the ``graphiti-core`` SDK initialisation, lifecycle management,
and the primary read/write operations used by the ETL pipeline and graph
query tools.

The client is initialised once during FastAPI startup (lifespan) and shared
across the application.  It reuses the existing ``Neo4jSettings`` for
connection parameters.
"""

from __future__ import annotations

from core.logging_utils import get_logger
from dataclasses import dataclass, field
from typing import Any

from config.settings import Settings

_logger = get_logger(__name__)


@dataclass
class EpisodeData:
    """Input payload for :meth:`GraphitiClient.add_episode`."""

    name: str
    body: str
    source: str = "etl"
    source_description: str = ""
    reference_time: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class GraphitiClient:
    """Thin wrapper around the ``graphiti-core`` SDK.

    Attributes:
        _graphiti: The underlying ``Graphiti`` instance (or ``None`` before
            :meth:`initialize` is called).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._graphiti: Any | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Connect to Neo4j and initialise the Graphiti instance.

        Must be called during application startup.  Raises if the
        ``graphiti-core`` package is not installed or Neo4j is unreachable.
        """
        neo4j = self._settings.neo4j
        if not neo4j.enabled:
            _logger.warning("Neo4j is disabled — GraphitiClient will not connect")
            return

        try:
            from graphiti_core import Graphiti  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "graphiti-core is required for the ETL module. "
                "Install it with: pip install graphiti-core"
            ) from exc

        _logger.info("Initialising Graphiti client (uri=%s) ...", neo4j.uri)

        # Build LLM + embedder + reranker using the project's config.
        llm_client = self._build_llm_client()
        embedder = self._build_embedder()
        cross_encoder = self._build_cross_encoder()

        self._graphiti = Graphiti(
            neo4j.uri,
            neo4j.username,
            neo4j.password,
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=cross_encoder,
        )
        await self._graphiti.build_indices_and_constraints()
        _logger.info("Graphiti client initialised")

    async def close(self) -> None:
        """Release connection resources.  Safe to call multiple times."""
        if self._graphiti is not None:
            await self._graphiti.close()
            self._graphiti = None
            _logger.info("Graphiti client closed")

    @property
    def is_connected(self) -> bool:
        return self._graphiti is not None

    # ------------------------------------------------------------------
    # Write operations (ETL pipeline)
    # ------------------------------------------------------------------

    async def add_episode(self, data: EpisodeData) -> None:
        """Write a single episode (Graphiti's basic data unit)."""
        self._ensure_connected()
        from datetime import datetime, timezone

        from graphiti_core.utils.maintenance.graph_data_operations import EpisodeType

        ref_time = (
            datetime.fromisoformat(data.reference_time)
            if data.reference_time
            else datetime.now(timezone.utc)
        )

        await self._graphiti.add_episode(
            name=data.name,
            episode_body=data.body,
            source=EpisodeType.text,
            source_description=data.source_description,
            reference_time=ref_time,
        )

    # ------------------------------------------------------------------
    # Read operations (graph query tools)
    # ------------------------------------------------------------------

    async def search(
        self, query: str, *, num_results: int = 10
    ) -> list[dict[str, Any]]:
        """Hybrid semantic + graph-traversal search."""
        self._ensure_connected()
        results = await self._graphiti.search(query, num_results=num_results)
        return [self._fact_to_dict(r) for r in results]

    async def get_entity(
        self, entity_type: str, entity_id: str
    ) -> dict[str, Any] | None:
        """Look up a single entity node by type and ID."""
        self._ensure_connected()
        query = f"{entity_type} {entity_id}"
        results = await self._graphiti.search(query, num_results=1)
        if not results:
            return None
        return self._fact_to_dict(results[0])

    async def get_relationships(
        self, entity_type: str, entity_id: str, *, depth: int = 2
    ) -> list[dict[str, Any]]:
        """Retrieve the relationship network around an entity."""
        self._ensure_connected()
        query = f"relationships of {entity_type} {entity_id}"
        results = await self._graphiti.search(query, num_results=depth * 5)
        return [self._fact_to_dict(r) for r in results]

    # ------------------------------------------------------------------
    # Direct Neo4j operations (edge creation)
    # ------------------------------------------------------------------

    async def clear_graph(self) -> None:
        """Delete all nodes, relationships, and indexes from Neo4j.

        Called before a full ETL sync to ensure a clean slate.
        """
        self._ensure_connected()
        driver = self._graphiti.driver
        _logger.warning("Clearing all Neo4j data ...")
        await driver.execute_query("MATCH (n) DETACH DELETE n")
        # Re-build Graphiti indexes
        await self._graphiti.build_indices_and_constraints()
        _logger.info("Neo4j cleared and indexes rebuilt")

    async def execute_cypher(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a raw Cypher query against Neo4j.

        Used by the ETL pipeline to create edges after all nodes have been
        written via episodes.
        """
        self._ensure_connected()
        driver = self._graphiti.driver
        result = await driver.execute_query(query, params=parameters or {})
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_llm_client(self) -> Any:
        """Build an OpenAI-compatible LLM client for Graphiti.

        Uses :class:`QwenCompatibleClient` which falls back to
        ``chat.completions`` + JSON mode instead of the ``responses.parse``
        API, making it compatible with Qwen/Dashscope and other
        non-OpenAI endpoints.
        """
        from graphiti_core.llm_client.config import LLMConfig

        from core.etl.llm_client import QwenCompatibleClient

        llm_cfg = self._settings.llm_fast
        if not llm_cfg.api_key:
            llm_cfg = self._settings.llm

        config = LLMConfig(
            api_key=llm_cfg.api_key,
            base_url=llm_cfg.api_base or None,
            model=llm_cfg.model,
            small_model=llm_cfg.model,  # same model for small tasks
        )
        return QwenCompatibleClient(config)

    def _build_cross_encoder(self) -> Any:
        """Build an OpenAI-compatible cross-encoder (reranker) for Graphiti."""
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

        llm_cfg = self._settings.llm_fast
        if not llm_cfg.api_key:
            llm_cfg = self._settings.llm

        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=llm_cfg.api_key,
            base_url=llm_cfg.api_base or None,
        )
        return OpenAIRerankerClient(client=client)

    def _build_embedder(self) -> Any:
        """Build an OpenAI-compatible embedder for Graphiti.

        Uses the same API credentials as the LLM client. The embedding
        model defaults to ``text-embedding-3-small`` unless the project
        uses a different provider that supports ``/embeddings``.
        """
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

        llm_cfg = self._settings.llm_fast
        if not llm_cfg.api_key:
            llm_cfg = self._settings.llm

        # Use the embedding model from project config if available,
        # otherwise fall back to text-embedding-v3 (Dashscope compatible).
        embedding_model = getattr(
            self._settings, "embedding_model", None
        ) or "text-embedding-v3"

        config = OpenAIEmbedderConfig(
            api_key=llm_cfg.api_key,
            base_url=llm_cfg.api_base or None,
            embedding_model=embedding_model,
        )
        inner = OpenAIEmbedder(config)

        # Wrap with batch-splitting for Dashscope's 10-item limit
        from graphiti_core.embedder.client import EmbedderClient

        from core.etl.llm_client import DashscopeCompatibleEmbedder

        # Register as virtual subclass so isinstance(wrapper, EmbedderClient) is True
        EmbedderClient.register(DashscopeCompatibleEmbedder)

        return DashscopeCompatibleEmbedder(inner)

    def _ensure_connected(self) -> None:
        if self._graphiti is None:
            raise RuntimeError(
                "GraphitiClient is not initialised. "
                "Call 'await client.initialize()' first."
            )

    @staticmethod
    def _fact_to_dict(fact: Any) -> dict[str, Any]:
        """Convert a Graphiti fact/edge object to a plain dict."""
        if hasattr(fact, "model_dump"):
            return fact.model_dump()
        if hasattr(fact, "__dict__"):
            return {k: v for k, v in fact.__dict__.items() if not k.startswith("_")}
        return {"value": str(fact)}
