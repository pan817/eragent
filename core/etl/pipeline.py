"""ETL pipeline orchestrator.

Coordinates Extract → Transform → Load for full and incremental syncs,
managing per-table watermarks via :class:`SyncStateManager`.
"""

from __future__ import annotations

import asyncio
from core.logging_utils import get_logger
import time
from datetime import datetime
from typing import Any

from core.etl.extractors.base import BaseExtractor
from core.etl.loaders.graphiti_loader import GraphitiLoader
from core.etl.metrics import etl_metrics
from core.etl.models import SyncResult
from core.etl.state import SyncStateManager
from core.etl.tracing import etl_tracer
from core.etl.transformers.llm_extractor import LLMTextExtractor
from core.etl.transformers.structured import StructuredTransformer

_logger = get_logger(__name__)

# Domain execution order — parent data first so edges can resolve.
_DOMAIN_ORDER = ["master_data", "purchasing", "receiving", "payables", "sourcing"]


class ETLPipeline:
    """Orchestrate full and incremental ETL syncs."""

    def __init__(
        self,
        extractors: dict[str, BaseExtractor],
        transformer: StructuredTransformer,
        loader: GraphitiLoader,
        state_manager: SyncStateManager,
        llm_extractor: LLMTextExtractor | None = None,
        max_concurrent_domains: int = 3,
    ) -> None:
        self._extractors = extractors
        self._transformer = transformer
        self._loader = loader
        self._state = state_manager
        self._llm_extractor = llm_extractor
        self._max_concurrent = max_concurrent_domains

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def needs_full_sync(self) -> bool:
        """Check whether any domain table is missing a watermark."""
        all_tables: list[str] = []
        for ext in self._extractors.values():
            all_tables.extend(ext.table_names())
        return await self._state.needs_full_sync(all_tables)

    async def run_full_sync(self) -> SyncResult:
        """Execute a full sync: clear Neo4j, then rebuild all domains."""
        _logger.info("ETL full sync started — clearing Neo4j graph")
        t0 = time.monotonic()
        result = SyncResult(sync_type="full")

        # Clear Neo4j before full rebuild
        await self._loader._client.clear_graph()

        with etl_tracer.sync("full") as sync_id:
            for domain_name in _DOMAIN_ORDER:
                ext = self._extractors.get(domain_name)
                if ext is None:
                    continue
                await self._sync_domain(
                    ext, sync_type="FULL", result=result, sync_id=sync_id
                )

        result.duration_ms = (time.monotonic() - t0) * 1000
        status = "success" if not result.failed_tables else "partial_failure"
        etl_metrics.inc_sync_total("full", status)
        etl_metrics.record_sync_duration("full", result.duration_ms / 1000)
        _logger.info(
            "ETL full sync completed: rows=%d nodes=%d edges=%d duration_ms=%.0f failed=%s",
            result.total_rows,
            result.total_nodes,
            result.total_edges,
            result.duration_ms,
            result.failed_tables or "none",
        )
        return result

    async def run_incremental_sync(self) -> SyncResult:
        """Execute an incremental sync: domains in parallel (bounded)."""
        _logger.info("ETL incremental sync started")
        t0 = time.monotonic()

        sem = asyncio.Semaphore(self._max_concurrent)

        # Use a shared sync_id for the trace
        _sync_ctx = {}

        async def _run(domain_name: str) -> SyncResult:
            async with sem:
                partial = SyncResult(sync_type="incremental")
                ext = self._extractors.get(domain_name)
                if ext is not None:
                    await self._sync_domain(
                        ext,
                        sync_type="INCREMENTAL",
                        result=partial,
                        sync_id=_sync_ctx.get("sync_id"),
                    )
                return partial

        with etl_tracer.sync("incremental") as sync_id:
            _sync_ctx["sync_id"] = sync_id
            tasks = [_run(d) for d in _DOMAIN_ORDER if d in self._extractors]
            partials = await asyncio.gather(*tasks, return_exceptions=True)

        merged = SyncResult(sync_type="incremental")
        for p in partials:
            if isinstance(p, BaseException):
                _logger.error("ETL domain sync raised: %s", p, exc_info=p)
                continue
            merged.total_rows += p.total_rows
            merged.total_nodes += p.total_nodes
            merged.total_edges += p.total_edges
            merged.failed_tables.extend(p.failed_tables)
            merged.succeeded_tables.extend(p.succeeded_tables)

        merged.duration_ms = (time.monotonic() - t0) * 1000
        status = "success" if not merged.failed_tables else "partial_failure"
        etl_metrics.inc_sync_total("incremental", status)
        etl_metrics.record_sync_duration("incremental", merged.duration_ms / 1000)
        _logger.info(
            "ETL incremental sync completed: rows=%d nodes=%d edges=%d duration_ms=%.0f failed=%s",
            merged.total_rows,
            merged.total_nodes,
            merged.total_edges,
            merged.duration_ms,
            merged.failed_tables or "none",
        )
        return merged

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _sync_domain(
        self,
        extractor: BaseExtractor,
        sync_type: str,
        result: SyncResult,
        sync_id: str | None = None,
    ) -> None:
        """Sync all tables in a single domain (sequential within domain)."""
        domain = extractor.domain()
        domain_span_id = f"domain:{domain}"

        with etl_tracer.span("domain", domain, sync_id or "") as domain_attrs:
            for table_name in extractor.table_names():
                try:
                    await self._sync_table(
                        extractor, table_name, domain, sync_type, result,
                        sync_id=sync_id, parent_span_id=domain_span_id,
                    )
                    result.succeeded_tables.append(table_name)
                except Exception as exc:
                    result.failed_tables.append(table_name)
                    etl_metrics.inc_errors(domain, type(exc).__name__)
                    _logger.error(
                        "ETL sync failed for %s.%s",
                        domain,
                        table_name,
                    exc_info=True,
                )

    async def _sync_table(
        self,
        extractor: BaseExtractor,
        table_name: str,
        domain: str,
        sync_type: str,
        result: SyncResult,
        sync_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> None:
        """Extract, transform, and load one table."""
        table_t0 = time.monotonic()
        # Mark RUNNING
        now = datetime.utcnow()
        await self._state.update_watermark(
            table_name=table_name,
            domain=domain,
            watermark=now,
            rows_synced=0,
            sync_type=sync_type,
            status="RUNNING",
        )

        total_rows = 0
        total_nodes = 0
        total_edges = 0
        max_watermark: datetime | None = None
        pending_edges: list[Any] = []  # collect edges for Cypher creation

        # Choose iterator
        if sync_type == "FULL":
            row_iter = extractor.extract_full(table_name)
        else:
            watermark = await self._state.get_watermark(table_name)
            if watermark is None:
                row_iter = extractor.extract_full(table_name)
            else:
                row_iter = extractor.extract_incremental(table_name, watermark)

        async for batch in row_iter:
            transform_result = self._transformer.transform(table_name, batch)

            if transform_result.nodes:
                load_result = await self._loader.load(
                    transform_result.nodes, transform_result.edges
                )
                total_nodes += load_result.loaded

            # Collect edges for post-load Cypher creation
            pending_edges.extend(transform_result.edges)

            # Free text → Graphiti add_episode (LLM extraction)
            if transform_result.free_text_records:
                try:
                    await self._loader.load_free_text(
                        transform_result.free_text_records
                    )
                except Exception:
                    _logger.warning(
                        "Free text loading failed for %s batch",
                        table_name,
                        exc_info=True,
                    )

            total_rows += len(batch)

            # Track max watermark from batch
            for row in batch:
                lud = row.get("last_update_date")
                if lud is not None:
                    if isinstance(lud, datetime):
                        if max_watermark is None or lud > max_watermark:
                            max_watermark = lud

        # Update state
        final_watermark = max_watermark or now
        await self._state.update_watermark(
            table_name=table_name,
            domain=domain,
            watermark=final_watermark,
            rows_synced=total_rows,
            sync_type=sync_type,
            status="SUCCESS",
        )

        # Create edges via Cypher after all nodes are written
        if pending_edges:
            edges_created = await self._loader.create_edges(pending_edges)
            total_edges = edges_created

        result.total_rows += total_rows
        result.total_nodes += total_nodes
        result.total_edges += total_edges

        # Record per-table metrics
        etl_metrics.inc_rows_extracted(domain, table_name, total_rows)

        # Record trace span with real duration
        if sync_id:
            table_duration = (time.monotonic() - table_t0) * 1000
            etl_tracer.record_span(
                "table", table_name, sync_id,
                duration_ms=table_duration,
                parent_id=parent_span_id,
                rows=total_rows, nodes=total_nodes, edges=total_edges,
            )

        _logger.info(
            "ETL table synced: %s rows=%d nodes=%d edges=%d",
            table_name,
            total_rows,
            total_nodes,
            total_edges,
        )
