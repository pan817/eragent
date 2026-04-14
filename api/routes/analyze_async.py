"""异步分析任务路由：提交 / 查询 / SSE 事件流。

与旧的同步 ``/analyze`` 端点互补：
- ``POST /analyze/async`` 立即返回 ``trace_id`` 与两个 chat_message_id
- ``GET /analyze/tasks/{trace_id}`` 查询任务快照（TTL 内有完整 result）
- ``GET /analyze/tasks/{trace_id}/events`` 订阅 SSE 事件流
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.routes.analyze import _get_orchestrator, _run_db_io
from api.schemas.analysis import AnalysisRequest, AnalysisResult, AnalysisStatus
from config.settings import get_settings
from core.chat import get_chat_repository
from core.logging_utils import get_logger
from core.observability.tables import TraceRun
from core.tasks import (
    AnalysisTaskAck,
    AnalysisTaskSnapshot,
    TaskEntry,
    TaskState,
    get_event_bus,
    get_task_registry,
)

_logger = get_logger(__name__)

router = APIRouter(tags=["analysis-async"])


def _sse_heartbeat_seconds() -> float:
    try:
        return float(get_settings().async_analysis.sse_heartbeat_sec)
    except Exception:  # noqa: BLE001
        return 15.0


# ---------------------------------------------------------------------------
# 路由端点
# ---------------------------------------------------------------------------


@router.post("/analyze/async", response_model=AnalysisTaskAck, status_code=202)
async def analyze_async(
    request: AnalysisRequest, http_request: Request
) -> AnalysisTaskAck:
    """提交异步分析任务并立即返回 trace_id。

    - 生成 trace_id 与 session_id（若未指定）
    - ``auto_persist=True`` 时预写 user/assistant 消息，assistant 为 ``pending``
    - 登记任务到 TaskRegistry，后台执行 orchestrator.analyze()
    - 响应包含轮询 URL 与 SSE URL，前端据此订阅进度
    """
    registry = get_task_registry()
    if registry is None:
        raise HTTPException(status_code=503, detail="task registry not initialized")

    # 自动生成 session_id
    if not request.session_id:
        request = request.model_copy(update={"session_id": str(uuid.uuid4())})
    trace_id = str(uuid.uuid4())

    user_message_id: str | None = None
    assistant_message_id: str | None = None

    chat_repo = get_chat_repository()
    if request.auto_persist and chat_repo:
        await _ensure_session_async(
            chat_repo, request.user_id, request.session_id
        )
        if request.regenerate_of:
            updated = await _run_db_io(
                chat_repo.update_message,
                request.user_id,
                request.session_id,
                request.regenerate_of,
                {
                    "content": "",
                    "status": "pending",
                    "trace_id": trace_id,
                },
            )
            if updated is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"assistant message {request.regenerate_of} 不存在",
                )
            assistant_message_id = request.regenerate_of
        else:
            user_msg = {
                "role": "user",
                "content": request.query,
                "client_id": request.client_user_message_id,
                "status": "success",
                "metadata": request.metadata,
            }
            asst_msg = {
                "role": "assistant",
                "content": "",
                "client_id": request.client_assistant_message_id,
                "status": "pending",
                "trace_id": trace_id,
                "metadata": request.metadata,
            }
            append_result = await _run_db_io(
                chat_repo.append_messages,
                request.user_id,
                request.session_id,
                [user_msg, asst_msg],
            )
            if append_result:
                msgs = append_result.get("messages", [])
                if len(msgs) >= 1:
                    user_message_id = msgs[0].get("id")
                if len(msgs) >= 2:
                    assistant_message_id = msgs[1].get("id")

    runner = _build_runner(
        request=request,
        chat_repo=chat_repo,
        assistant_message_id=assistant_message_id,
        user_message_id=user_message_id,
    )

    entry = await registry.submit(
        request,
        trace_id=trace_id,
        assistant_message_id=assistant_message_id,
        user_message_id=user_message_id,
        runner_factory=runner,
    )

    # 使用 FastAPI 的 url_path_for 自动拼接 router 挂载 prefix
    poll_url = http_request.url_for(
        "get_task_snapshot", trace_id=entry.trace_id
    ).path
    stream_url = http_request.url_for(
        "stream_task_events", trace_id=entry.trace_id
    ).path
    return AnalysisTaskAck(
        trace_id=entry.trace_id,
        status=entry.state,
        session_id=request.session_id,
        user_id=request.user_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        poll_url=poll_url,
        stream_url=stream_url,
        created_at=entry.created_at,
    )


@router.get(
    "/analyze/tasks/{trace_id}",
    response_model=AnalysisTaskSnapshot,
)
async def get_task_snapshot(trace_id: str) -> AnalysisTaskSnapshot:
    """查询任务快照：优先读 TaskRegistry 内存 entry，miss 回落 trace_runs 表。"""
    registry = get_task_registry()
    if registry is not None:
        entry = registry.get(trace_id)
        if entry is not None:
            return _entry_to_snapshot(entry)

    snapshot = await asyncio.to_thread(_load_snapshot_from_db, trace_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"任务 {trace_id} 不存在")
    return snapshot


@router.get("/analyze/tasks/{trace_id}/events")
async def stream_task_events(
    trace_id: str,
    request: Request,
    last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """SSE 事件流。断线重连可通过 ``Last-Event-ID`` 请求头重放。"""
    bus = get_event_bus()
    if bus is None:
        raise HTTPException(status_code=503, detail="event bus not initialized")

    async def event_stream() -> AsyncIterator[bytes]:
        last_sent_seq = last_event_id if isinstance(last_event_id, int) else 0
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)
        heartbeat = _sse_heartbeat_seconds()

        async def pump() -> None:
            try:
                async for ev in bus.subscribe(trace_id, last_event_id=last_sent_seq):
                    await queue.put(ev)
            finally:
                await queue.put(None)

        pump_task = asyncio.create_task(pump(), name=f"sse-pump:{trace_id}")
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=heartbeat
                    )
                except asyncio.TimeoutError:
                    yield _format_heartbeat(trace_id)
                    continue
                if item is None:
                    return
                yield _format_event(item)
                if item.get("type") == "done":
                    return
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


async def _ensure_session_async(repo: Any, user_id: str, session_id: str) -> None:
    """确保 chat_session 存在（与同步端点一致的创建逻辑）。"""
    existing = await _run_db_io(repo.get_session, user_id, session_id)
    if existing is None:
        await _run_db_io(repo.create_session_with_id, user_id, session_id)


def _build_runner(
    *,
    request: AnalysisRequest,
    chat_repo: Any,
    assistant_message_id: str | None,
    user_message_id: str | None,
):
    """返回一个闭包，供 TaskRegistry 作为 runner_factory 调用。"""

    orchestrator = _get_orchestrator()

    async def runner(entry: TaskEntry) -> AnalysisResult:
        result = await orchestrator.analyze(request, trace_id=entry.trace_id)

        # 统一给 result 附上 chat message id，便于前端直接消费
        if assistant_message_id is not None:
            result.assistant_message_id = assistant_message_id
        if user_message_id is not None:
            result.user_message_id = user_message_id

        # 更新 assistant 消息：pending → success / error
        if chat_repo is not None and assistant_message_id is not None:
            content = result.report_markdown or (
                result.error.message if result.error else "(无内容)"
            )
            msg_status = (
                "error" if result.status == AnalysisStatus.FAILED else "success"
            )
            try:
                await _run_db_io(
                    chat_repo.update_message,
                    request.user_id,
                    request.session_id,
                    assistant_message_id,
                    {
                        "content": content,
                        "status": msg_status,
                        "duration_ms": int(result.duration_ms or 0),
                        "trace_id": result.trace_id or entry.trace_id,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "failed to finalize assistant message %s: %s",
                    assistant_message_id,
                    exc,
                )

        return result

    return runner


def _entry_to_snapshot(entry: TaskEntry) -> AnalysisTaskSnapshot:
    return AnalysisTaskSnapshot(
        trace_id=entry.trace_id,
        status=entry.state,
        session_id=entry.session_id,
        user_id=entry.user_id,
        created_at=entry.created_at,
        started_at=entry.started_at,
        finished_at=entry.finished_at,
        duration_ms=entry.duration_ms,
        stage=entry.stage,
        result=entry.result,
        error=entry.error,
    )


def _load_snapshot_from_db(trace_id: str) -> AnalysisTaskSnapshot | None:
    """TaskRegistry 内存 miss 时的兜底：从 trace_runs 构造轻量快照。"""
    # 沿用 analyze.py 中的 orchestrator 持有的 session_factory 太绕，
    # 这里直接从全局 TraceStore 的 session_factory 间接拿数据。
    from core.observability.store import get_trace_store

    store = get_trace_store()
    if store is None:
        return None
    with store._session_factory() as session:  # noqa: SLF001 (内部 API，可接受)
        run: TraceRun | None = session.get(TraceRun, trace_id)
        if run is None:
            return None
        state = _trace_status_to_task_state(run.status)
        from api.schemas.analysis import ErrorInfo

        error = (
            ErrorInfo(code="TASK_ERROR", message=run.error or "任务失败")
            if run.error or state in (TaskState.ERROR, TaskState.ABORTED)
            else None
        )
        return AnalysisTaskSnapshot(
            trace_id=run.trace_id,
            status=state,
            session_id=run.session_id,
            user_id=run.user_id,
            created_at=run.started_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_ms=run.duration_ms,
            stage=None,
            result=None,  # 完整 result 已不在内存；前端需改走 /reports
            error=error,
        )


def _trace_status_to_task_state(status: str) -> TaskState:
    mapping = {
        "success": TaskState.OK,
        "ok": TaskState.OK,
        "running": TaskState.RUNNING,
        "error": TaskState.ERROR,
        "aborted": TaskState.ABORTED,
    }
    return mapping.get(status, TaskState.ERROR)


def _format_event(event: dict[str, Any]) -> bytes:
    """序列化单条事件为标准 SSE 帧。"""
    ev_type = str(event.get("type", "message"))
    seq = event.get("seq")
    lines: list[str] = [f"event: {ev_type}"]
    if isinstance(seq, int):
        lines.append(f"id: {seq}")
    lines.append(f"data: {json.dumps(event, ensure_ascii=False, default=str)}")
    lines.append("")  # 终止空行
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _format_heartbeat(trace_id: str) -> bytes:
    payload = {"type": "heartbeat", "trace_id": trace_id}
    return (
        "event: heartbeat\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode("utf-8")
