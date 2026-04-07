"""TimingMiddleware：基于 LangChain 1.2 AgentMiddleware 钩子做耗时埋点。

设计要点：
- `before_agent` / `after_agent` 在 LangGraph 中作为独立节点运行，跨节点的
  contextvars 不一定共享。因此 trace 生命周期由调用方（P2PAgent.analyze）
  通过 `start_run()` / `finish_run()` 显式驱动。
- `wrap_model_call` / `wrap_tool_call` 在对应节点内同步调用，可直接读取
  调用线程的 contextvar，正确归属到当前 trace。
"""

from __future__ import annotations

import contextvars
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ToolCallRequest,
)

from core.observability.console import format_summary, format_tree
from core.observability.store import RunEvent, SpanEvent, TraceStore, get_trace_store


# ---------------------------------------------------------------------------
# Trace 上下文
# ---------------------------------------------------------------------------


class _TraceContext:
    """单次 agent 调用的运行时上下文。"""

    def __init__(self, agent_name: str, session_id: str | None, user_id: str | None) -> None:
        self.trace_id: str = str(uuid.uuid4())
        self.agent_name: str = agent_name
        self.session_id: str | None = session_id
        self.user_id: str | None = user_id
        self.spans: list[SpanEvent] = []
        self.started_at: datetime = datetime.utcnow()
        self.started_monotonic: float = time.monotonic()
        self.model_count: int = 0
        self.tool_count: int = 0


_current_trace: contextvars.ContextVar[_TraceContext | None] = contextvars.ContextVar(
    "current_trace", default=None
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class TimingMiddleware(AgentMiddleware):
    """链路耗时埋点中间件。"""

    name = "timing_middleware"

    def __init__(
        self,
        *,
        agent_name: str = "agent",
        store: TraceStore | None = None,
        print_console: bool = True,
    ) -> None:
        super().__init__()
        self._agent_name = agent_name
        self._store_override = store
        self._print = print_console

    # ------------------------------------------------------------------
    # Trace 生命周期（由 P2PAgent 等调用方显式驱动）
    # ------------------------------------------------------------------

    def start_run(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """开启一次 trace，返回 trace_id。"""
        ctx = _TraceContext(
            agent_name=self._agent_name,
            session_id=session_id,
            user_id=user_id,
        )
        _current_trace.set(ctx)
        self._emit(
            RunEvent(
                kind="run_start",
                trace_id=ctx.trace_id,
                agent_name=ctx.agent_name,
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                started_at=ctx.started_at,
            )
        )
        return ctx.trace_id

    def finish_run(self, *, status: str = "success", error: str | None = None) -> None:
        """结束当前 trace，写出 agent 顶层 span 与 run_end 事件。"""
        ctx = _current_trace.get()
        if ctx is None:
            return
        finished_at = datetime.utcnow()
        total_ms = (time.monotonic() - ctx.started_monotonic) * 1000
        agent_span = SpanEvent(
            trace_id=ctx.trace_id,
            span_id=str(uuid.uuid4()),
            parent_span_id=None,
            span_type="agent",
            name=ctx.agent_name,
            status=status,
            started_at=ctx.started_at,
            finished_at=finished_at,
            duration_ms=round(total_ms, 3),
            attributes={
                "model_calls": ctx.model_count,
                "tool_calls": ctx.tool_count,
            },
            error=error,
        )
        ctx.spans.append(agent_span)
        self._emit(agent_span)
        self._emit(
            RunEvent(
                kind="run_end",
                trace_id=ctx.trace_id,
                agent_name=ctx.agent_name,
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                started_at=ctx.started_at,
                finished_at=finished_at,
                duration_ms=round(total_ms, 3),
                status=status,
                error=error,
                model_call_count=ctx.model_count,
                tool_call_count=ctx.tool_count,
            )
        )
        if self._print:
            tree_spans = [s for s in ctx.spans if s.span_type != "agent"]
            print(format_tree(tree_spans, ctx.trace_id), flush=True)
            print(format_summary(tree_spans, total_ms), flush=True)
        _current_trace.set(None)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _store(self) -> TraceStore | None:
        return self._store_override or get_trace_store()

    def _emit(self, ev: RunEvent | SpanEvent) -> None:
        store = self._store()
        if store is not None:
            store.enqueue(ev)

    @contextmanager
    def _span(self, span_type: str, name: str, attributes: dict[str, Any]):
        ctx = _current_trace.get()
        if ctx is None:
            yield None
            return
        span_id = str(uuid.uuid4())
        started_at = datetime.utcnow()
        t0 = time.monotonic()
        status = "ok"
        error: str | None = None
        try:
            yield span_id
        except BaseException as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1000
            sp = SpanEvent(
                trace_id=ctx.trace_id,
                span_id=span_id,
                parent_span_id=None,  # 由调用方在 finish_run 时归并到 agent root
                span_type=span_type,
                name=name,
                status=status,
                started_at=started_at,
                finished_at=datetime.utcnow(),
                duration_ms=round(duration_ms, 3),
                attributes=attributes,
                error=error,
            )
            ctx.spans.append(sp)
            if span_type == "model":
                ctx.model_count += 1
            elif span_type == "tool":
                ctx.tool_count += 1
            self._emit(sp)

    # ------------------------------------------------------------------
    # model hooks
    # ------------------------------------------------------------------

    def wrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        attrs = self._model_attrs(request)
        with self._span("model", attrs["model"], attrs):
            return handler(request)

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        attrs = self._model_attrs(request)
        with self._span("model", attrs["model"], attrs):
            return await handler(request)

    @staticmethod
    def _model_attrs(request: ModelRequest) -> dict[str, Any]:
        model_name = (
            getattr(request.model, "model_name", None)
            or getattr(request.model, "model", None)
            or type(request.model).__name__
        )
        return {
            "model": str(model_name),
            "message_count": len(request.messages or []),
            "tool_count": len(request.tools or []),
        }

    # ------------------------------------------------------------------
    # tool hooks
    # ------------------------------------------------------------------

    def wrap_tool_call(  # type: ignore[override]
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        attrs = self._tool_attrs(request)
        with self._span("tool", attrs["tool"], attrs):
            return handler(request)

    async def awrap_tool_call(  # type: ignore[override]
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        attrs = self._tool_attrs(request)
        with self._span("tool", attrs["tool"], attrs):
            return await handler(request)

    @staticmethod
    def _tool_attrs(request: ToolCallRequest) -> dict[str, Any]:
        call = request.tool_call or {}
        return {
            "tool": call.get("name", "unknown"),
            "tool_call_id": call.get("id"),
            "args": call.get("args"),
        }
