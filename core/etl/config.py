"""Graphiti ETL configuration.

All ETL-specific settings are grouped here and mounted as
``Settings.graphiti_etl`` in the root settings object.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class GraphitiETLSettings(BaseSettings):
    """Graphiti ETL configuration knobs."""

    # --- master switch ---
    enabled: bool = True

    # --- query backend ---
    query_backend: str = "graphiti"  # graphiti / postgresql / hybrid

    # --- sync scheduling ---
    sync_interval_seconds: int = 600
    full_sync_on_startup: bool = True
    batch_size: int = 500
    max_concurrent_domains: int = 3

    # --- timeout & retry ---
    sync_timeout_seconds: int = 3600
    incremental_timeout_seconds: int = 300
    retry_max_attempts: int = 3
    retry_backoff_seconds: float = 5.0

    # --- LLM extraction (optional enhancement) ---
    llm_extraction_enabled: bool = False
    llm_extraction_batch_size: int = 20
    llm_extraction_model: str = ""  # empty -> fall back to llm_fast

    # --- graph search ---
    search_default_top_k: int = 10
    search_timeout_seconds: int = 30
    context_enrichment_enabled: bool = True

    # --- loader batching & concurrency ---
    episode_batch_size: int = 50        # rows merged into one episode body
    max_load_concurrency: int = 5       # concurrent add_episode calls

    # --- per-table row limit (testing / validation) ---
    # 0 = unlimited; >0 = cap rows extracted per table per sync.
    # Useful for smoke-testing ETL with a small dataset.
    max_rows_per_table: int = 10

    # --- resource limits ---
    max_nodes_per_sync: int = 50_000
    max_edges_per_sync: int = 100_000

    model_config = {"populate_by_name": True, "env_prefix": "ETL_"}
