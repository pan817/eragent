"""ETL metrics — lightweight in-memory counters and gauges.

Provides thread-safe metric collection for ETL sync operations.
Metrics are exposed via the Admin API ``GET /admin/etl/metrics`` endpoint.

Design note: We use simple dicts + threading.Lock instead of a full
metrics library (Prometheus/OpenTelemetry) to match the project's
existing observability style (logging + in-memory stats).
"""

from __future__ import annotations

import threading
import time
from typing import Any


class _Metrics:
    """Thread-safe ETL metrics collector."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Counters
        self._sync_total: dict[str, int] = {}  # key: "{sync_type}:{status}"
        self._rows_extracted: dict[str, int] = {}  # key: "{domain}:{table}"
        self._nodes_loaded: dict[str, int] = {}  # key: node_type
        self._edges_loaded: dict[str, int] = {}  # key: edge_type
        self._errors: dict[str, int] = {}  # key: "{domain}:{error_type}"
        self._llm_calls: int = 0
        self._llm_tokens: int = 0
        # Histograms (simplified: store last N durations)
        self._sync_durations: list[tuple[str, float]] = []  # (sync_type, seconds)
        # Gauges
        self._watermark_lag: dict[str, float] = {}  # key: table_name, value: seconds

    # ── Counter increments ────────────────────────────────────

    def inc_sync_total(self, sync_type: str, status: str) -> None:
        key = f"{sync_type}:{status}"
        with self._lock:
            self._sync_total[key] = self._sync_total.get(key, 0) + 1

    def inc_rows_extracted(self, domain: str, table: str, count: int) -> None:
        key = f"{domain}:{table}"
        with self._lock:
            self._rows_extracted[key] = self._rows_extracted.get(key, 0) + count

    def inc_nodes_loaded(self, node_type: str, count: int) -> None:
        with self._lock:
            self._nodes_loaded[node_type] = self._nodes_loaded.get(node_type, 0) + count

    def inc_edges_loaded(self, edge_type: str, count: int) -> None:
        with self._lock:
            self._edges_loaded[edge_type] = self._edges_loaded.get(edge_type, 0) + count

    def inc_errors(self, domain: str, error_type: str) -> None:
        key = f"{domain}:{error_type}"
        with self._lock:
            self._errors[key] = self._errors.get(key, 0) + 1

    def inc_llm_calls(self, count: int = 1) -> None:
        with self._lock:
            self._llm_calls += count

    def inc_llm_tokens(self, count: int) -> None:
        with self._lock:
            self._llm_tokens += count

    # ── Histogram / Gauge ─────────────────────────────────────

    def record_sync_duration(self, sync_type: str, duration_seconds: float) -> None:
        with self._lock:
            self._sync_durations.append((sync_type, duration_seconds))
            # Keep last 100 entries
            if len(self._sync_durations) > 100:
                self._sync_durations = self._sync_durations[-100:]

    def set_watermark_lag(self, table: str, lag_seconds: float) -> None:
        with self._lock:
            self._watermark_lag[table] = lag_seconds

    # ── Snapshot ──────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of all metrics."""
        with self._lock:
            return {
                "etl_sync_total": dict(self._sync_total),
                "etl_rows_extracted": dict(self._rows_extracted),
                "etl_nodes_loaded": dict(self._nodes_loaded),
                "etl_edges_loaded": dict(self._edges_loaded),
                "etl_errors": dict(self._errors),
                "etl_llm_calls": self._llm_calls,
                "etl_llm_tokens": self._llm_tokens,
                "etl_sync_durations_recent": [
                    {"sync_type": t, "duration_seconds": round(d, 3)}
                    for t, d in self._sync_durations[-10:]
                ],
                "etl_watermark_lag_seconds": dict(self._watermark_lag),
            }

    def reset(self) -> None:
        """Reset all metrics (testing)."""
        with self._lock:
            self._sync_total.clear()
            self._rows_extracted.clear()
            self._nodes_loaded.clear()
            self._edges_loaded.clear()
            self._errors.clear()
            self._llm_calls = 0
            self._llm_tokens = 0
            self._sync_durations.clear()
            self._watermark_lag.clear()


# Module-level singleton
etl_metrics = _Metrics()
