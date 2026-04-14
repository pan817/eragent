#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
WORKERS="${WORKERS:-4}"
LOG_LEVEL="${LOG_LEVEL:-info}"

# 同时把 WORKERS 暴露给应用，以便 lifespan 启动时做 event_backend 一致性检查。
# uvicorn 自身也会识别 WEB_CONCURRENCY，这里二者同值，避免不一致。
export WEB_CONCURRENCY="$WORKERS"

# 提醒：WORKERS>1 时必须在 config.yaml 中配置
#   async_analysis.event_backend: "redis"
#   async_analysis.redis_url: "redis://<host>:6379/0"
# 否则 POST /analyze/async 与 GET /analyze/tasks/{id}/events 落在不同 worker
# 进程时 SSE 将只看到心跳、收不到业务事件（lifespan 会打 WARNING）。

cd "$PROJECT_ROOT"

exec uvicorn api.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --workers "$WORKERS" \
    --log-level "$LOG_LEVEL"
