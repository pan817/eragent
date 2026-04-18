"""Admin metrics routes (route hit rate dashboard)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query

from core.logging_utils import get_logger

_logger = get_logger(__name__)

router = APIRouter(prefix="/admin/metrics", tags=["admin"])


def _parse_window(window: str) -> int:
    """Parse window string like '7d' / '30d' to days. Default 7."""
    m = re.match(r"^(\d+)d$", window.strip())
    if m:
        return min(int(m.group(1)), 365)
    return 7


@router.get("/route-hit-rate")
async def route_hit_rate(
    window: str = Query(default="7d", description="Time window, e.g. 1d/7d/30d"),
) -> list[dict[str, Any]]:
    """Return route hit rate distribution from trace_spans."""
    from core.database.engine import get_engine
    from sqlalchemy import text

    days = _parse_window(window)
    engine = get_engine()

    sql = text("""
        SELECT
            attributes->>'hit_level'   AS route_level,
            attributes->>'result_type' AS analysis_type,
            attributes->>'execution'   AS execution_path,
            COUNT(*)                   AS count,
            ROUND(AVG((attributes->>'confidence')::numeric), 3) AS avg_confidence
        FROM trace_spans
        WHERE span_type = 'intent'
          AND span_name = 'route_decision'
          AND start_time >= NOW() - make_interval(days => :days)
        GROUP BY 1, 2, 3
        ORDER BY count DESC
    """)

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"days": days}).mappings().all()
            return [dict(r) for r in rows]
    except Exception as exc:
        _logger.warning("route-hit-rate query failed: %s", exc)
        return []
