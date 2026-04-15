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
    TERMINAL_STATES,
    AnalysisTaskAck,
    AnalysisTaskSnapshot,
    TaskEntry,
    TaskState,
    get_event_bus,
    get_task_registry,
)
from core.time_utils import now_cn

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

    _logger.info(
        "POST /analyze/async inbound: user=%s session=%s trace_id=%s "
        "regenerate_of=%s query_len=%d",
        request.user_id, request.session_id, trace_id,
        request.regenerate_of or "-",
        len(request.query or ""),
    )

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
    _logger.info(
        "POST /analyze/async accepted: trace_id=%s state=%s user=%s session=%s",
        entry.trace_id, entry.state.value if hasattr(entry.state, "value") else entry.state,
        request.user_id, request.session_id,
    )
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
    snapshot = await asyncio.to_thread(_resolve_task_snapshot, trace_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"任务 {trace_id} 不存在")
    return snapshot


@router.get("/analyze/tasks/{trace_id}/events")
async def stream_task_events(
    trace_id: str,
    request: Request,
    last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """SSE 事件流。断线重连可通过 ``Last-Event-ID`` 请求头重放。

    健壮性保证（三道闸，详见 docs/issue/async_analyze_backend_issue.md）：
    - 闸 1：握手前校验 trace_id 存在性，未知 trace_id 直接 404
    - 闸 2：连接建立后立刻合成一条 ``status`` 快照作为第一帧
      （不依赖 buffer replay，避免 buffer 已 drop 的场景空响应）
    - 闸 3：退出前若未发过 ``done``，从 registry / trace_runs 合成一条终态事件
      或 ``error`` 事件，确保前端任何情况下都能收到终结帧
    """
    bus = get_event_bus()
    if bus is None:
        raise HTTPException(status_code=503, detail="event bus not initialized")

    # 闸 1：握手前校验 trace_id（registry miss 回落 trace_runs）
    initial_snapshot = await asyncio.to_thread(
        _resolve_task_snapshot, trace_id
    )
    if initial_snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"任务 {trace_id} 不存在："
                "未提交、trace_id 错误，或已超过快照 TTL 被清理"
            ),
        )

    async def event_stream() -> AsyncIterator[bytes]:
        last_sent_seq = last_event_id if isinstance(last_event_id, int) else 0
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)
        heartbeat = _sse_heartbeat_seconds()
        done_emitted = False

        # 闸 2：连接建立后立即合成 status 快照作为第一帧。
        # 若任务已终态且 buffer 已 drop，buffer replay 拿不到东西，
        # 这里保证前端至少能先看到一帧当前状态。seq=0 不冲击业务 seq 序列。
        yield _format_event(_synthesize_status_event(trace_id, initial_snapshot))

        # 任务已经处于终态：不再订阅 live 流，直接合成 done 后结束。
        # 这覆盖了"已终结 + buffer 已过期"的场景。
        if initial_snapshot.status in TERMINAL_STATES:
            yield _format_event(
                _synthesize_done_event(trace_id, initial_snapshot)
            )
            return

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
                    break
                yield _format_event(item)
                if item.get("type") == "done":
                    done_emitted = True
                    return
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

            # 闸 3：尾部兜底。如果走到这里还没发过 done，
            # 合成一个终态事件送出去——避免前端永远等不到终结帧。
            # 客户端断开时 yield 会抛异常，忽略即可（已经没人听）。
            if not done_emitted and not await request.is_disconnected():
                try:
                    fallback = await asyncio.to_thread(
                        _resolve_task_snapshot, trace_id
                    )
                    if fallback is not None and fallback.status in TERMINAL_STATES:
                        yield _format_event(
                            _synthesize_done_event(trace_id, fallback)
                        )
                    else:
                        yield _format_event(
                            _synthesize_error_event(
                                trace_id,
                                code="STREAM_DROPPED",
                                message=(
                                    "事件流在任务终结前断开，请通过 "
                                    "GET /analyze/tasks/{trace_id} 查询最终状态"
                                ),
                            )
                        )
                except Exception:  # noqa: BLE001
                    # 兜底失败不抛，避免把 generator 弄成 error state
                    _logger.exception(
                        "failed to synthesize terminal event for trace %s",
                        trace_id,
                    )

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
    """TaskRegistry 内存 miss 时的兜底：从 trace_runs 构造快照，
    终态成功时**再顺带**从 reports 表按 trace_id 反查完整 AnalysisResult。

    跨 worker 场景（POST 落 A / GET 落 B）下，这是前端拿到 ``result.report_markdown``
    的唯一路径——以前这里硬编码 ``result=None`` 导致前端看到 "status=ok, result=null"
    被误判为"分析失败"。
    """
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
        # 状态一致性守门：trace_runs 还是 running，但 EventBus 的 closed 标志已置位，
        # 意味着 registry 那侧任务已经终结（done 已发），只是 run_end 的 flush 还没
        # commit 到 DB（registry 的 flush 屏障应当已经处理大多数 race，这里是剩余
        # 时序残差的 belt-and-suspenders）。记一条 INFO 方便未来定位；状态不在此
        # 处强改，交给前端 "done + 1s × 3 retry" 兜底或下一次刷新自然收敛。
        if run.status == "running":
            bus = get_event_bus()
            if bus is not None and bus.is_closed(trace_id):
                _logger.info(
                    "trace_runs shows running but event bus closed for trace %s; "
                    "run_end flush not yet committed (expected: flush barrier "
                    "should have waited; check trace-store latency)",
                    trace_id,
                )
        state = _trace_status_to_task_state(run.status)
        from api.schemas.analysis import ErrorInfo

        error = (
            ErrorInfo(code="TASK_ERROR", message=run.error or "任务失败")
            if run.error or state in (TaskState.ERROR, TaskState.ABORTED)
            else None
        )

        # 成功终态：按 trace_id 反查 reports，rehydrate 完整 AnalysisResult。
        # 失败 / 非终态不查（reports 表只在成功时被写入）。
        result = (
            _hydrate_result_from_reports(trace_id)
            if state == TaskState.OK
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
            result=result,
            error=error,
        )


def _hydrate_result_from_reports(trace_id: str) -> AnalysisResult | None:
    """按 trace_id 从 reports 表反序列化完整 AnalysisResult。

    orchestrator._persist_report 在每次成功的 analyze 里同步写 reports 表，
    所以 SSE done 发出时这里一定已经有行（没有新的 race）。

    返回 None 的情况（属于合理降级，不 log ERROR）：
    - is_recall=True 的回溯查询：跳过 _persist_report，reports 里没有行
    - _persist_report 自身失败：已经在 orchestrator 那里打过 WARNING 日志
    - 迁移前创建的历史行：trace_id 列为 NULL
    """
    try:
        from core.memory import get_long_term_memory
    except Exception:  # noqa: BLE001
        return None
    ltm = get_long_term_memory()
    if ltm is None or not hasattr(ltm, "get_report_by_trace_id"):
        return None
    try:
        row = ltm.get_report_by_trace_id(trace_id)
    except Exception:  # noqa: BLE001
        _logger.warning(
            "hydrate result: reports lookup failed for trace %s",
            trace_id,
            exc_info=True,
        )
        return None
    if row is None:
        return None
    raw_json = row.get("result_json")
    if not raw_json:
        return None
    try:
        return AnalysisResult.model_validate_json(raw_json)
    except Exception:  # noqa: BLE001
        _logger.warning(
            "hydrate result: failed to parse result_json for trace %s "
            "(reports.id=%s)",
            trace_id,
            row.get("id"),
            exc_info=True,
        )
        return None


def _trace_status_to_task_state(status: str) -> TaskState:
    # "queued" 来自 registry.submit() 在 POST 阶段种下的占位行，
    # orchestrator.start_run 之后会被 merge 成 "running"。没有这条映射
    # 会被 .get(default=ERROR) 当成失败误报。
    mapping = {
        "queued": TaskState.QUEUED,
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
    """序列化一条 heartbeat。带上 type/trace_id/ts/seq 四字段（seq 恒为 0）。

    - heartbeat **不占用** 业务 seq 计数器（Redis 后端下 INCR 是跨进程全局的，
      让心跳消耗业务序列既浪费又会把断点续传的锚点带偏）。固定写 ``seq: 0``
      表示"非业务事件"，前端不应据此更新 Last-Event-ID。
    - 不输出 SSE ``id:`` 行：浏览器 EventSource 不应把心跳 seq 作为
      Last-Event-ID 锚点（心跳不需重放，业务事件才需要重放）。
    """
    payload = {
        "type": "heartbeat",
        "trace_id": trace_id,
        "ts": now_cn().isoformat(),
        "seq": 0,
    }
    return (
        "event: heartbeat\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


def _resolve_task_snapshot(trace_id: str) -> AnalysisTaskSnapshot | None:
    """统一解析任务快照：先查 registry（最新内存态），miss 回落 trace_runs。

    供 SSE 握手校验（闸 1）、首帧合成（闸 2）、尾部兜底（闸 3）共用。
    registry 的 entry 即使任务已完成也会在 TTL 内保留；DB 侧 trace_runs 是
    长期权威；TTL 过期后的请求走 DB 回落拿到结构化终态。
    """
    registry = get_task_registry()
    if registry is not None:
        entry = registry.get(trace_id)
        if entry is not None:
            return _entry_to_snapshot(entry)
    return _load_snapshot_from_db(trace_id)


def _synthesize_status_event(
    trace_id: str, snapshot: AnalysisTaskSnapshot
) -> dict[str, Any]:
    """基于快照合成一条 status 事件（SSE 连接首帧）。

    ``seq=0`` + ``synthesized=True`` 向前端表明这是服务端合成帧，不冲击
    断点续传锚点；状态以快照为准（queued / running / ok / error / aborted）。
    """
    return {
        "type": "status",
        "trace_id": trace_id,
        "ts": now_cn().isoformat(),
        "seq": 0,
        "state": snapshot.status.value,
        "stage": snapshot.stage,
        "synthesized": True,
    }


def _synthesize_done_event(
    trace_id: str, snapshot: AnalysisTaskSnapshot
) -> dict[str, Any]:
    """基于终态快照合成一条 done 事件。调用方必须先校验 snapshot 处于终态。"""
    payload: dict[str, Any] = {
        "type": "done",
        "trace_id": trace_id,
        "ts": now_cn().isoformat(),
        "seq": 0,
        "status": snapshot.status.value,
        "duration_ms": snapshot.duration_ms,
        "synthesized": True,
    }
    if snapshot.result is not None:
        payload["anomaly_count"] = len(snapshot.result.anomalies)
    if snapshot.error is not None:
        payload["error"] = snapshot.error.model_dump()
    return payload


def _synthesize_error_event(
    trace_id: str, *, code: str, message: str
) -> dict[str, Any]:
    """合成一条 error 事件。仅在无法合成 done（状态未知）时使用。"""
    return {
        "type": "error",
        "trace_id": trace_id,
        "ts": now_cn().isoformat(),
        "seq": 0,
        "code": code,
        "message": message,
        "synthesized": True,
    }
