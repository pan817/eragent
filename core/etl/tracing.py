"""ETL-specific tracing — independent from TimingMiddleware.

Provides a lightweight span tree for each ETL sync run. Spans are stored
in memory (last N runs) and printed as a tree to the log on completion.
Exposed via ``GET /admin/etl/traces``.

Usage in pipeline::

    with etl_tracer.sync("full") as sync_id:
        with etl_tracer.span("domain", "master_data", sync_id):
            with etl_tracer.span("table", "AP_SUPPLIERS", sync_id, rows=5):
                ...
"""

from __future__ import annotations

from core.logging_utils import get_logger
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Generator

_logger = get_logger(__name__)

_MAX_RECENT_TRACES = 20


@dataclass
class ETLSpan:
    """A single span within an ETL trace."""

    span_id: str
    name: str  # e.g. "domain:master_data", "table:AP_SUPPLIERS", "load", "edges"
    parent_id: str | None
    started_at: float  # monotonic
    duration_ms: float = 0.0
    status: str = "ok"  # ok / failed
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class ETLTrace:
    """A complete trace for one ETL sync run."""

    sync_id: str
    sync_type: str  # full / incremental
    started_at: float
    duration_ms: float = 0.0
    status: str = "running"  # running / ok / failed
    spans: list[ETLSpan] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sync_id": self.sync_id,
            "sync_type": self.sync_type,
            "duration_ms": round(self.duration_ms, 1),
            "status": self.status,
            "span_count": len(self.spans),
            "spans": [
                {
                    "name": s.name,
                    "parent": s.parent_id,
                    "duration_ms": round(s.duration_ms, 1),
                    "status": s.status,
                    **s.attributes,
                }
                for s in self.spans
            ],
        }


class ETLTracer:
    """Lightweight ETL trace collector, independent of TimingMiddleware."""

    def __init__(self, max_traces: int = _MAX_RECENT_TRACES) -> None:
        self._lock = Lock()
        self._traces: deque[ETLTrace] = deque(maxlen=max_traces)
        self._active: dict[str, ETLTrace] = {}  # sync_id -> trace

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @contextmanager
    def sync(self, sync_type: str) -> Generator[str, None, None]:
        """Top-level context manager for a sync run.

        Yields the ``sync_id``. On exit, finalises the trace and logs
        the span tree.
        """
        sync_id = str(uuid.uuid4())[:8]
        trace = ETLTrace(
            sync_id=sync_id,
            sync_type=sync_type,
            started_at=time.monotonic(),
        )
        with self._lock:
            self._active[sync_id] = trace

        try:
            yield sync_id
            trace.status = "ok"
        except Exception:
            trace.status = "failed"
            raise
        finally:
            trace.duration_ms = (time.monotonic() - trace.started_at) * 1000
            with self._lock:
                self._active.pop(sync_id, None)
                self._traces.append(trace)
            self._log_tree(trace)

    @contextmanager
    def span(
        self,
        category: str,
        name: str,
        sync_id: str,
        parent_id: str | None = None,
        **attributes: Any,
    ) -> Generator[dict[str, Any], None, None]:
        """Record a child span within a sync run.

        Yields a mutable ``attributes`` dict so callers can add data
        (rows, nodes, edges) during execution.
        """
        span_id = f"{category}:{name}"
        s = ETLSpan(
            span_id=span_id,
            name=span_id,
            parent_id=parent_id,
            started_at=time.monotonic(),
            attributes=dict(attributes),
        )

        try:
            yield s.attributes
            s.status = "ok"
        except Exception:
            s.status = "failed"
            raise
        finally:
            s.duration_ms = (time.monotonic() - s.started_at) * 1000
            with self._lock:
                trace = self._active.get(sync_id)
                if trace is not None:
                    trace.spans.append(s)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def record_span(
        self,
        category: str,
        name: str,
        sync_id: str,
        duration_ms: float,
        status: str = "ok",
        parent_id: str | None = None,
        **attributes: Any,
    ) -> None:
        """Record a completed span with known duration (no context manager)."""
        span_id = f"{category}:{name}"
        s = ETLSpan(
            span_id=span_id,
            name=span_id,
            parent_id=parent_id,
            started_at=0,
            duration_ms=duration_ms,
            status=status,
            attributes=dict(attributes),
        )
        with self._lock:
            trace = self._active.get(sync_id)
            if trace is not None:
                trace.spans.append(s)

    def get_recent_traces(self) -> list[dict[str, Any]]:
        """Return recent traces as dicts (newest first)."""
        with self._lock:
            return [t.to_dict() for t in reversed(self._traces)]

    # ------------------------------------------------------------------
    # Log output
    # ------------------------------------------------------------------

    @staticmethod
    def _log_tree(trace: ETLTrace) -> None:
        """Print a human-readable span tree to the log."""
        status_icon = "✓" if trace.status == "ok" else "✗"
        lines = [
            f"=== ETL sync {trace.sync_id} ({trace.sync_type}) "
            f"{status_icon} {trace.status}  {trace.duration_ms:.0f}ms ==="
        ]

        # Group spans by parent
        children: dict[str | None, list[ETLSpan]] = {}
        for s in trace.spans:
            children.setdefault(s.parent_id, []).append(s)

        def _render(parent_id: str | None, prefix: str = "") -> None:
            kids = children.get(parent_id, [])
            for i, s in enumerate(kids):
                is_last = i == len(kids) - 1
                connector = "└─" if is_last else "├─"
                s_icon = "✓" if s.status == "ok" else "✗"
                attr_str = "  ".join(
                    f"{k}={v}" for k, v in s.attributes.items()
                )
                lines.append(
                    f"{prefix}{connector} [{s.name}] {s_icon}  "
                    f"{s.duration_ms:.0f}ms  {attr_str}"
                )
                child_prefix = prefix + ("   " if is_last else "│  ")
                _render(s.span_id, child_prefix)

        _render(None)

        # Summary
        total_rows = sum(s.attributes.get("rows", 0) for s in trace.spans)
        total_nodes = sum(s.attributes.get("nodes", 0) for s in trace.spans)
        total_edges = sum(s.attributes.get("edges", 0) for s in trace.spans)
        failed = [s.name for s in trace.spans if s.status == "failed"]
        lines.append(
            f"--- summary: total={trace.duration_ms:.0f}ms  "
            f"rows={total_rows}  nodes={total_nodes}  edges={total_edges}  "
            f"failed={failed or 'none'} ---"
        )

        # Print to stderr directly (not via logger) to avoid being
        # filtered by log-level config.  Mirrors core/observability/console.py.
        import sys

        output = "\n".join(lines)
        print(output, file=sys.stderr, flush=True)
        _logger.info(output)


# Module-level singleton
etl_tracer = ETLTracer()
