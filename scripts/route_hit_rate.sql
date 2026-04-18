-- Route hit rate aggregation (7-day window)
-- Run: psql -f scripts/route_hit_rate.sql
SELECT
    attributes->>'hit_level'   AS route_level,
    attributes->>'result_type' AS analysis_type,
    attributes->>'execution'   AS execution_path,
    COUNT(*)                   AS count,
    ROUND(AVG((attributes->>'confidence')::numeric), 3) AS avg_confidence,
    ROUND(
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY EXTRACT(EPOCH FROM (end_time - start_time)) * 1000
        )::numeric, 1
    ) AS p50_duration_ms
FROM trace_spans
WHERE span_type = 'intent'
  AND span_name = 'route_decision'
  AND start_time >= NOW() - INTERVAL '7 days'
GROUP BY 1, 2, 3
ORDER BY count DESC;
