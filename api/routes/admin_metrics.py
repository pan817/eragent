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


@router.get("/chat-index-status")
async def chat_index_status() -> dict[str, Any]:
    """Chat 索引覆盖率：已索引片段数 / 总 chat_messages 数。"""
    from sqlalchemy import text

    from core.database.engine import get_engine

    engine = get_engine()
    result: dict[str, Any] = {
        "total_messages": 0,
        "dead_letter_count": 0,
        "indexer_queue_depth": 0,
    }
    try:
        with engine.connect() as conn:
            result["total_messages"] = conn.execute(
                text("SELECT COUNT(*) FROM chat_messages")
            ).scalar() or 0
            result["dead_letter_count"] = conn.execute(
                text("SELECT COUNT(*) FROM chat_index_dead_letter")
            ).scalar() or 0
    except Exception as exc:
        _logger.warning("chat-index-status query failed: %s", exc)

    try:
        from core.memory import get_chat_indexer

        indexer = get_chat_indexer()
        if indexer is not None:
            result["indexer_queue_depth"] = len(indexer._queue)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("chat indexer queue check failed: %s", exc)

    return result


@router.get("/chat-search-quality")
async def chat_search_quality(
    window: str = Query(default="7d", description="Time window, e.g. 1d/7d/30d"),
) -> dict[str, Any]:
    """Chat history 检索命中率分布（exact / semantic / 0 hit）。"""
    from sqlalchemy import text

    from core.database.engine import get_engine

    days = _parse_window(window)
    engine = get_engine()

    result: dict[str, Any] = {
        "window_days": days,
        "exact_hits": 0,
        "semantic_hits": 0,
        "zero_hits": 0,
        "total_searches": 0,
    }
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT
                        COALESCE(attributes->>'channel_a_hits', '0') AS a_hits,
                        COALESCE(attributes->>'channel_b_hits', '0') AS b_hits
                    FROM trace_spans
                    WHERE span_type = 'memory'
                      AND span_name = 'memory.chat.search'
                      AND start_time >= NOW() - make_interval(days => :days)
                """),
                {"days": days},
            ).fetchall()

            for row in rows:
                a = int(row.a_hits or 0)
                b = int(row.b_hits or 0)
                result["total_searches"] += 1
                if a > 0:
                    result["exact_hits"] += 1
                if b > 0:
                    result["semantic_hits"] += 1
                if a == 0 and b == 0:
                    result["zero_hits"] += 1
    except Exception as exc:
        _logger.warning("chat-search-quality query failed: %s", exc)

    return result


@router.get("/session-recap-coverage")
async def session_recap_coverage() -> dict[str, Any]:
    """Session recap 覆盖率：已摘要会话数 / 总会话数。"""
    from sqlalchemy import text

    from core.database.engine import get_engine

    engine = get_engine()
    result: dict[str, Any] = {
        "total_sessions": 0,
        "summarized_sessions": 0,
        "coverage_pct": 0.0,
    }
    try:
        with engine.connect() as conn:
            result["total_sessions"] = conn.execute(
                text("SELECT COUNT(*) FROM chat_sessions WHERE deleted_at IS NULL")
            ).scalar() or 0
            result["summarized_sessions"] = conn.execute(
                text("SELECT COUNT(*) FROM session_summaries")
            ).scalar() or 0
            if result["total_sessions"] > 0:
                result["coverage_pct"] = round(
                    result["summarized_sessions"] / result["total_sessions"] * 100, 1,
                )
    except Exception as exc:
        _logger.warning("session-recap-coverage query failed: %s", exc)

    return result
