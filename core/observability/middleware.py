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

from core.logging_utils import get_logger, get_trace_logger
from core.observability.console import format_io_panel, format_summary, format_tree
from core.observability.store import RunEvent, SpanEvent, TraceStore, get_trace_store
from core.time_utils import now_cn

_logger = get_logger(__name__)
_trace_logger = get_trace_logger()


# 单条 input/output 文本的最大长度。超出截断，避免 attributes JSON 膨胀。
# 默认 2000，可被 ObservabilitySettings.max_io_text 覆盖。
_MAX_IO_TEXT_DEFAULT = 2000

# 单条 error 文本（含完整异常链 traceback）的最大长度。超出截断，
# 避免极端栈撑爆 trace_spans.error 列与 API 响应。可被
# ObservabilitySettings.max_error_text 覆盖。
_MAX_ERROR_TEXT_DEFAULT = 8192

# token 估算默认比率（字符数 / 此比率 ≈ token 数，中文约 1.5）
_DEFAULT_TOKEN_RATIO = 1.5


def _max_io_text() -> int:
    try:
        from config.settings import get_settings

        return get_settings().observability.max_io_text
    except Exception as exc:
        _logger.info("failed to load observability.max_io_text, using default: %s", exc)
        return _MAX_IO_TEXT_DEFAULT


def _max_error_text() -> int:
    try:
        from config.settings import get_settings

        return get_settings().observability.max_error_text
    except Exception as exc:
        _logger.info(
            "failed to load observability.max_error_text, using default: %s", exc
        )
        return _MAX_ERROR_TEXT_DEFAULT


def _console_enabled() -> bool:
    """控制台 trace 输出总开关（默认开）。"""
    try:
        from config.settings import get_settings

        return bool(get_settings().observability.console_enabled)
    except Exception as exc:
        _logger.info("failed to load observability.console_enabled: %s", exc)
        return True


def _console_io_panel() -> bool:
    """每次 model/tool 调用的 I/O 面板开关（默认关，排查时开启）。"""
    try:
        from config.settings import get_settings

        return bool(get_settings().observability.console_io_panel)
    except Exception as exc:
        _logger.info("failed to load observability.console_io_panel: %s", exc)
        return False


def _slow_tool_ms() -> int:
    try:
        from config.settings import get_settings

        return int(get_settings().observability.slow_tool_ms)
    except Exception as exc:
        _logger.info("failed to load observability.slow_tool_ms: %s", exc)
        return 2000


def _slow_model_ms() -> int:
    try:
        from config.settings import get_settings

        return int(get_settings().observability.slow_model_ms)
    except Exception as exc:
        _logger.info("failed to load observability.slow_model_ms: %s", exc)
        return 5000


def _verbose_calls() -> bool:
    """高频调用 INFO 开关（LLM / Tool / Memory / checkpointer）。

    默认开启便于生产排查。高并发场景下运维可把
    ``observability.verbose_calls`` 改为 ``false``，相关成功路径 INFO 将
    彻底静默（不打 DEBUG——项目策略不设 DEBUG 日志）。
    """
    try:
        from config.settings import get_settings

        return bool(get_settings().observability.verbose_calls)
    except Exception as exc:
        _logger.info("failed to load observability.verbose_calls: %s", exc)
        return True


def format_error_chain(exc: BaseException, *, max_len: int | None = None) -> str:
    """格式化异常为 trace error 字符串。

    与 ``traceback.format_exc(limit=3)`` 不同，此函数：
    - 通过 ``traceback.format_exception(..., chain=True)`` 完整记录
      ``__cause__`` / ``__context__`` 链，不丢失被包装的根因；
    - 不限制栈帧数量，深栈调用（agent → SDK → httpx → asyncio）也能完整保留；
    - 总长度超过 ``max_len`` 时执行头尾截断（默认头 60% + 尾 40%），
      中间插入 ``...[truncated N chars; full length=M]...`` 标记，
      同时保住链顶根因与链尾外层异常。

    Args:
        exc: 待格式化的异常。
        max_len: 上限字符数。``None`` 表示读取
            ``ObservabilitySettings.max_error_text``（默认 8192）。
    """
    if max_len is None:
        max_len = _max_error_text()
    text = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__, chain=True)
    ).rstrip()
    if len(text) <= max_len:
        return text
    truncated_n = len(text) - max_len
    marker = f"\n...[truncated {truncated_n} chars; full length={len(text)}]...\n"
    budget = max_len - len(marker)
    if budget < 200:
        # 上限太小，无法做有意义的头尾切分，退化为单纯头截断
        return text[: max(0, max_len - 3)] + "..."
    head_len = (budget * 6) // 10
    tail_len = budget - head_len
    truncated_n = len(text) - head_len - tail_len
    marker = f"\n...[truncated {truncated_n} chars; full length={len(text)}]...\n"
    # marker 长度变动后再校准一次 head，确保总长不超 max_len
    head_len = max_len - len(marker) - tail_len
    return text[:head_len] + marker + text[-tail_len:]


def _token_estimate_ratio() -> float:
    try:
        from config.settings import get_settings

        return get_settings().llm.token_estimate_ratio
    except Exception as exc:
        _logger.info("failed to load llm.token_estimate_ratio, using default: %s", exc)
        return _DEFAULT_TOKEN_RATIO


def _classify_llm_error(exc: BaseException) -> str:
    """将 LLM 调用异常分类为可读的错误类型标签。"""
    exc_type = type(exc).__name__
    exc_module = type(exc).__module__ or ""
    exc_str = str(exc).lower()

    # 超时类
    if isinstance(exc, (TimeoutError, OSError)) or "timeout" in exc_type.lower():
        return "timeout"
    if "timeout" in exc_str or "timed out" in exc_str:
        return "timeout"

    # 连接类
    if "connect" in exc_str or "connection" in exc_type.lower():
        return "connection_error"

    # 速率限制
    if "rate" in exc_str and "limit" in exc_str:
        return "rate_limit"
    if "429" in exc_str or "ratelimit" in exc_type.lower():
        return "rate_limit"

    # 认证
    if "auth" in exc_str or "401" in exc_str or "apikey" in exc_str:
        return "auth_error"

    # 模型服务端错误
    if any(code in exc_str for code in ("500", "502", "503", "504")):
        return "server_error"

    # openai / httpx 系异常
    if "openai" in exc_module or "httpx" in exc_module:
        return f"api_error.{exc_type}"

    return exc_type


def estimate_tokens(text: str | None) -> int:
    """根据字符数估算 token 数量。

    使用可配置的字符/token 比率，适用于中文为主的文本。
    不依赖外部 tokenizer，轻量且无第三方库依赖。
    """
    if not text:
        return 0
    return max(1, int(len(text) / _token_estimate_ratio()))


# ---------------------------------------------------------------------------
# Trace 上下文
# ---------------------------------------------------------------------------


class _TraceContext:
    """单次 agent 调用的运行时上下文。"""

    def __init__(
        self,
        agent_name: str,
        session_id: str | None,
        user_id: str | None,
        store: TraceStore | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.trace_id: str = trace_id or str(uuid.uuid4())
        self.agent_name: str = agent_name
        self.session_id: str | None = session_id
        self.user_id: str | None = user_id
        self.store: TraceStore | None = store
        self.spans: list[SpanEvent] = []
        self.started_at: datetime = now_cn()
        self.started_monotonic: float = time.monotonic()
        self.model_count: int = 0
        self.tool_count: int = 0
        self.memory_count: int = 0


_current_trace: contextvars.ContextVar[_TraceContext | None] = contextvars.ContextVar(
    "current_trace", default=None
)


# ---------------------------------------------------------------------------
# 事件总线桥接：把 span 起止 / 阶段节点同步广播给 EventBus
#
# 异步分析接口（/analyze/async）订阅这些事件走 SSE。observability 主流程
# 不依赖 EventBus，如果未初始化则所有 publish 为 no-op。
# ---------------------------------------------------------------------------


def _publish_to_event_bus(trace_id: str, payload: dict[str, Any]) -> None:
    """向全局 EventBus 发布事件。失败吞掉，不影响 observability 主流程。"""
    try:
        from core.tasks.events import get_event_bus

        bus = get_event_bus()
    except Exception:  # noqa: BLE001
        return
    if bus is None:
        return
    try:
        seq = bus.next_seq(trace_id)
        payload = {
            **payload,
            "trace_id": trace_id,
            "ts": now_cn().isoformat(),
            "seq": seq,
        }
        bus.publish(trace_id, payload)
    except Exception:  # noqa: BLE001
        # 不让事件广播失败影响主流程；降级为 debug 日志
        pass


def _publish_span_start(span_type: str, name: str) -> None:
    """在当前 trace 上发布 span 起始事件（仅 tool / dag.task）。"""
    ctx = _current_trace.get()
    if ctx is None:
        return
    from core.observability.display_labels import resolve_tool_label

    if span_type == "tool":
        _publish_to_event_bus(
            ctx.trace_id,
            {
                "type": "tool",
                "action": "start",
                "name": name,
                "label": resolve_tool_label(name),
            },
        )
    elif span_type == "dag.task":
        _publish_to_event_bus(
            ctx.trace_id,
            {
                "type": "dag_task",
                "action": "start",
                "task_name": name,
                "label": resolve_tool_label(name),
            },
        )


def _publish_span_end(
    span_type: str,
    name: str,
    *,
    duration_ms: float,
    status: str,
) -> None:
    """在当前 trace 上发布 span 结束事件（仅 tool / dag.task）。"""
    ctx = _current_trace.get()
    if ctx is None:
        return
    from core.observability.display_labels import resolve_tool_label

    if span_type == "tool":
        _publish_to_event_bus(
            ctx.trace_id,
            {
                "type": "tool",
                "action": "end",
                "name": name,
                "label": resolve_tool_label(name),
                "duration_ms": duration_ms,
                "status": status,
            },
        )
    elif span_type == "dag.task":
        _publish_to_event_bus(
            ctx.trace_id,
            {
                "type": "dag_task",
                "action": "end",
                "task_name": name,
                "label": resolve_tool_label(name),
                "duration_ms": duration_ms,
                "status": status,
            },
        )


def publish_stage(name: str, attrs: dict[str, Any] | None = None) -> None:
    """供编排层调用：发布阶段事件（intent_resolved / dag_planned / react_started 等）。

    在当前无活跃 trace 时 no-op。
    """
    ctx = _current_trace.get()
    if ctx is None:
        return
    from core.observability.display_labels import resolve_stage_label

    payload: dict[str, Any] = {
        "type": "stage",
        "name": name,
        "label": resolve_stage_label(name),
    }
    if attrs:
        payload["attrs"] = attrs
    _publish_to_event_bus(ctx.trace_id, payload)


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
        trace_id: str | None = None,
    ) -> str:
        """开启一次 trace，返回 trace_id。

        若传入 ``trace_id`` 则使用该值（用于异步分析场景下 API 层预生成
        trace_id 以便立即 ack 给客户端）；否则由内部生成 uuid4。
        """
        ctx = _TraceContext(
            agent_name=self._agent_name,
            session_id=session_id,
            user_id=user_id,
            store=self._store(),
            trace_id=trace_id,
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
        finished_at = now_cn()
        total_ms = (time.monotonic() - ctx.started_monotonic) * 1000

        # 汇总所有 model span 的 token 使用量
        total_prompt_tokens = 0
        total_completion_tokens = 0
        peak_prompt_tokens = 0
        context_budget: dict[str, Any] | None = None
        for sp in ctx.spans:
            if sp.span_type == "model":
                attrs = sp.attributes or {}
                # TimingMiddleware: usage 嵌套在 output dict 内
                # ReportAgent 手动 span: usage 在 attrs 顶层，output 是 string
                output = attrs.get("output")
                usage = output.get("usage") if isinstance(output, dict) else None
                if not isinstance(usage, dict):
                    usage = attrs.get("usage")
                if isinstance(usage, dict):
                    pt = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
                    ct = usage.get("output_tokens") or usage.get("completion_tokens") or 0
                    total_prompt_tokens += pt
                    total_completion_tokens += ct
                    if pt > peak_prompt_tokens:
                        peak_prompt_tokens = pt
            elif sp.span_type == "context_budget" and context_budget is None:
                context_budget = sp.attributes

        token_summary: dict[str, Any] = {
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "peak_prompt_tokens": peak_prompt_tokens,
        }
        if context_budget:
            token_summary["context_budget"] = context_budget

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
                "memory_calls": ctx.memory_count,
                "token_summary": token_summary,
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
        if self._print and _console_enabled():
            tree_spans = [s for s in ctx.spans if s.span_type != "agent"]
            # 把一次 trace 的 tree + summary 拼成单个字符串后一次性交给 trace logger。
            # StreamHandler.emit 内置锁保证整块原子写入，并发 trace 只会在块之间
            # 交错，不会在行内被业务日志切断。
            block = "\n".join(
                [
                    format_tree(tree_spans, ctx.trace_id),
                    format_summary(tree_spans, total_ms),
                ]
            )
            _trace_logger.info(block)
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
        started_at = now_cn()
        t0 = time.monotonic()
        status = "ok"
        error: str | None = None
        _publish_span_start(span_type, name)
        try:
            yield span_id
        except BaseException as exc:
            status = "error"
            error = format_error_chain(exc)
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
                finished_at=now_cn(),
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
            _publish_span_end(
                span_type, name,
                duration_ms=round(duration_ms, 3),
                status=status,
            )

    # ------------------------------------------------------------------
    # 通用 span 记录（带 input / output 捕获）
    # ------------------------------------------------------------------

    def _record_span(
        self,
        *,
        span_type: str,
        name: str,
        attributes: dict[str, Any],
        started_at: datetime,
        t0: float,
        status: str,
        error: str | None,
    ) -> None:
        ctx = _current_trace.get()
        duration_ms = (time.monotonic() - t0) * 1000
        sp = SpanEvent(
            trace_id=ctx.trace_id if ctx is not None else "orphan",
            span_id=str(uuid.uuid4()),
            parent_span_id=None,
            span_type=span_type,
            name=name,
            status=status,
            started_at=started_at,
            finished_at=now_cn(),
            duration_ms=round(duration_ms, 3),
            attributes=attributes,
            error=error,
        )
        if ctx is not None:
            ctx.spans.append(sp)
            if span_type == "model":
                ctx.model_count += 1
            elif span_type == "tool":
                ctx.tool_count += 1
        self._emit(sp)
        _publish_span_end(
            span_type, name,
            duration_ms=round(duration_ms, 3),
            status=status,
        )
        if self._print and _console_enabled() and _console_io_panel():
            # I/O 面板每次调用一个块，照样走 trace_logger 单次 info 原子输出。
            # 生产默认关闭（observability.console_io_panel=false），排查时开启。
            panel = format_io_panel(sp)
            if panel:
                _trace_logger.info(panel)

        # 高频调用成功 INFO：让排查时能直接通过业务日志看每次 LLM/Tool/Memory
        # 调用的关键指标，不用每次都翻 trace DB。受 observability.verbose_calls
        # 开关控制，高并发场景可一键静默。
        if status == "ok" and _verbose_calls():
            if span_type == "model":
                out = attributes.get("output") if isinstance(attributes.get("output"), dict) else {}
                usage = out.get("usage") or {} if isinstance(out, dict) else {}
                pt = usage.get("input_tokens") or usage.get("prompt_tokens")
                ct = usage.get("output_tokens") or usage.get("completion_tokens")
                _logger.info(
                    "model call ok: name=%s duration=%.1fms msgs=%s tools=%s "
                    "prompt_tokens=%s completion_tokens=%s",
                    name, duration_ms,
                    attributes.get("message_count"),
                    attributes.get("tool_count"),
                    pt, ct,
                )
            elif span_type == "tool":
                args_brief = _truncate_text(attributes.get("args"), 120)
                out_brief = _truncate_text(attributes.get("output"), 120)
                _logger.info(
                    "tool call ok: name=%s duration=%.1fms args=%s output=%s",
                    name, duration_ms, args_brief, out_brief,
                )
            elif span_type == "memory":
                _logger.info(
                    "memory op ok: name=%s duration=%.1fms attrs=%s",
                    name, duration_ms,
                    {k: v for k, v in attributes.items() if k not in ("input", "output")},
                )

        # 慢调用告警：tool / model 耗时超过阈值打 WARNING，辅助生产定位
        # 性能瓶颈（超时风险、速率限制、LLM 响应变慢等）。
        if status == "ok":
            if span_type == "tool":
                threshold = _slow_tool_ms()
                if threshold > 0 and duration_ms > threshold:
                    _logger.warning(
                        "slow tool: name=%s duration=%.1fms threshold=%dms",
                        name, duration_ms, threshold,
                    )
            elif span_type == "model":
                threshold = _slow_model_ms()
                if threshold > 0 and duration_ms > threshold:
                    _logger.warning(
                        "slow model: name=%s duration=%.1fms threshold=%dms "
                        "prompt_tokens=%s",
                        name, duration_ms, threshold,
                        (attributes.get("output") or {}).get("usage", {}).get(
                            "input_tokens"
                        ) if isinstance(attributes.get("output"), dict) else None,
                    )
        elif status == "error":
            # tool / model 失败已在 wrap_*_call 里抛异常，调用方可捕获。
            # 但当前并不自动写 WARNING 到业务日志，这里补一条简报，
            # 避免只有 trace DB 知道失败。
            _logger.warning(
                "span failed: type=%s name=%s duration=%.1fms error_type=%s",
                span_type, name, duration_ms,
                attributes.get("error_type") or "-",
            )

    # ------------------------------------------------------------------
    # model hooks（before/after model：捕获 input + output）
    # ------------------------------------------------------------------

    def wrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        attrs = self._model_attrs(request)
        attrs["input"] = self._serialize_model_input(request)
        started_at = now_cn()
        t0 = time.monotonic()
        status, error = "ok", None
        try:
            result = handler(request)
            attrs["output"] = self._serialize_model_output(result)
            return result
        except BaseException as exc:
            status = "error"
            attrs["error_type"] = _classify_llm_error(exc)
            attrs["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 2)
            error = format_error_chain(exc)
            raise
        finally:
            self._record_span(
                span_type="model",
                name=attrs["model"],
                attributes=attrs,
                started_at=started_at,
                t0=t0,
                status=status,
                error=error,
            )

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        attrs = self._model_attrs(request)
        attrs["input"] = self._serialize_model_input(request)
        started_at = now_cn()
        t0 = time.monotonic()
        status, error = "ok", None
        try:
            result = await handler(request)
            attrs["output"] = self._serialize_model_output(result)
            return result
        except BaseException as exc:
            status = "error"
            attrs["error_type"] = _classify_llm_error(exc)
            attrs["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 2)
            error = format_error_chain(exc)
            raise
        finally:
            self._record_span(
                span_type="model",
                name=attrs["model"],
                attributes=attrs,
                started_at=started_at,
                t0=t0,
                status=status,
                error=error,
            )

    @staticmethod
    def _model_attrs(request: ModelRequest) -> dict[str, Any]:
        model_name = (
            getattr(request.model, "model_name", None)
            or getattr(request.model, "model", None)
            or type(request.model).__name__
        )
        total_chars = 0

        # 1. system_message（独立字段，LLM 调用时合并到 messages 头部）
        sys_msg = getattr(request, "system_message", None)
        if sys_msg is not None:
            sys_content = getattr(sys_msg, "content", "")
            if isinstance(sys_content, str):
                total_chars += len(sys_content)
            elif sys_content is not None:
                total_chars += len(str(sys_content))

        # 2. messages（含 checkpointer 历史 + 注入消息 + 工具结果）
        for m in request.messages or []:
            content = getattr(m, "content", None)
            if isinstance(content, str):
                total_chars += len(content)
            elif content is not None:
                total_chars += len(str(content))

        # 3. tools（function schema，每次 LLM 调用都携带）
        tool_schema_chars = 0
        for t in request.tools or []:
            tool_schema_chars += len(getattr(t, "name", "") or "")
            tool_schema_chars += len(getattr(t, "description", "") or "")
            schema = getattr(t, "args_schema", None)
            if schema and callable(getattr(schema, "schema", None)):
                import json
                try:
                    tool_schema_chars += len(
                        json.dumps(schema.schema(), ensure_ascii=False)
                    )
                except Exception as exc:
                    _logger.warning(
                        "tool schema serialization failed for %s: %s",
                        getattr(t, "name", "unknown"),
                        exc,
                    )

        return {
            "model": str(model_name),
            "message_count": len(request.messages or []),
            "tool_count": len(request.tools or []),
            "estimated_input_tokens": estimate_tokens(
                "x" * (total_chars + tool_schema_chars)
            ),
        }

    @staticmethod
    def _serialize_model_input(request: ModelRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in request.messages or []:
            role = (
                getattr(m, "type", None)
                or getattr(m, "role", None)
                or type(m).__name__
            )
            content = getattr(m, "content", m)
            out.append(
                {
                    "role": str(role),
                    "content": _truncate_text(content),
                }
            )
        return out

    @staticmethod
    def _serialize_model_output(result: Any) -> dict[str, Any]:
        # LangChain 1.2 模型节点返回 {"messages": [AIMessage(...)]} 或类似结构
        # 也可能返回 ModelResponse(result=[AIMessage(...)]) dataclass
        msg: Any = result
        if isinstance(result, dict):
            msgs = result.get("messages") or result.get("result")
            if isinstance(msgs, list) and msgs:
                msg = msgs[-1]
            elif msgs is not None:
                msg = msgs
        elif isinstance(result, list) and result:
            msg = result[-1]
        else:
            # LangChain 1.2 ModelResponse dataclass: result 属性为 list[BaseMessage]
            result_list = getattr(result, "result", None)
            if isinstance(result_list, list) and result_list:
                msg = result_list[-1]

        content = getattr(msg, "content", None)
        tool_calls = getattr(msg, "tool_calls", None)
        usage = (
            getattr(msg, "usage_metadata", None)
            or getattr(msg, "response_metadata", None)
        )
        return {
            "content": _truncate_text(content if content is not None else str(msg)),
            "tool_calls": _safe_jsonable(tool_calls) if tool_calls else None,
            "usage": _safe_jsonable(usage) if usage else None,
        }

    # ------------------------------------------------------------------
    # tool hooks（before/after tool：捕获 args + 返回值）
    # ------------------------------------------------------------------

    def wrap_tool_call(  # type: ignore[override]
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        attrs = self._tool_attrs(request)
        attrs["input"] = attrs.get("args")
        started_at = now_cn()
        t0 = time.monotonic()
        status, error = "ok", None
        _publish_span_start("tool", attrs["tool"])
        try:
            result = handler(request)
            attrs["output"] = self._serialize_tool_output(result)
            return result
        except BaseException as exc:
            status = "error"
            error = format_error_chain(exc)
            raise
        finally:
            self._record_span(
                span_type="tool",
                name=attrs["tool"],
                attributes=attrs,
                started_at=started_at,
                t0=t0,
                status=status,
                error=error,
            )

    async def awrap_tool_call(  # type: ignore[override]
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        attrs = self._tool_attrs(request)
        attrs["input"] = attrs.get("args")
        started_at = now_cn()
        t0 = time.monotonic()
        status, error = "ok", None
        _publish_span_start("tool", attrs["tool"])
        try:
            result = await handler(request)
            attrs["output"] = self._serialize_tool_output(result)
            return result
        except BaseException as exc:
            status = "error"
            error = format_error_chain(exc)
            raise
        finally:
            self._record_span(
                span_type="tool",
                name=attrs["tool"],
                attributes=attrs,
                started_at=started_at,
                t0=t0,
                status=status,
                error=error,
            )

    @staticmethod
    def _tool_attrs(request: ToolCallRequest) -> dict[str, Any]:
        call = request.tool_call or {}
        return {
            "tool": call.get("name", "unknown"),
            "tool_call_id": call.get("id"),
            "args": _safe_jsonable(call.get("args")),
        }

    @staticmethod
    def _serialize_tool_output(result: Any) -> str:
        # tool handler 一般返回 ToolMessage；取其 content 字段
        content = getattr(result, "content", None)
        if content is None:
            content = result
        return _truncate_text(content)


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------


def _truncate_text(value: Any, max_len: int | None = None) -> str:
    if max_len is None:
        max_len = _max_io_text()
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            import json

            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception as exc:
            _logger.info("_truncate_text json serialization failed, falling back to str(): %s", exc)
            value = str(value)
    if len(value) > max_len:
        return value[: max_len - 3] + "..."
    return value


@contextmanager
def record_memory_span(operation: str, **attributes: Any):
    """在当前活跃 trace 中记录一次长期记忆操作 span（span_type="memory"）。

    供 core/memory 等模块在不持有 TimingMiddleware 引用时直接使用。
    若当前无活跃 trace，直接 yield 并跳过 span 记录，不影响业务逻辑。

    Args:
        operation: span 名称，建议用 "memory.read" / "memory.write" /
                   "memory.report_write" 等有层次的命名。
        **attributes: 写入 span attributes 的任意键值对（须为 JSON 可序列化类型）。

    Example::

        with record_memory_span("memory.write", user_id=uid, memory_type=mtype):
            repo.save(...)
    """
    ctx = _current_trace.get()
    if ctx is None:
        yield
        return

    span_id = str(uuid.uuid4())
    started_at = now_cn()
    t0 = time.monotonic()
    status = "ok"
    error: str | None = None
    try:
        yield
    except BaseException as exc:
        status = "error"
        error = format_error_chain(exc)
        raise
    finally:
        duration_ms = (time.monotonic() - t0) * 1000
        sp = SpanEvent(
            trace_id=ctx.trace_id,
            span_id=span_id,
            parent_span_id=None,
            span_type="memory",
            name=operation,
            status=status,
            started_at=started_at,
            finished_at=now_cn(),
            duration_ms=round(duration_ms, 3),
            attributes=attributes,
            error=error,
        )
        ctx.spans.append(sp)
        ctx.memory_count += 1
        active_store = ctx.store or get_trace_store()
        if active_store is not None:
            active_store.enqueue(sp)
        if status == "ok" and _verbose_calls():
            _logger.info(
                "memory op ok: name=%s duration=%.1fms attrs=%s",
                operation, duration_ms,
                {k: v for k, v in (attributes or {}).items()
                 if k not in ("input", "output")},
            )
        elif status == "error":
            _logger.warning(
                "memory op failed: name=%s duration=%.1fms",
                operation, duration_ms,
            )


@contextmanager
def record_span(span_type: str, name: str, **attributes: Any):
    """在当前活跃 trace 中记录一个自定义 span。

    通用版本的 span 记录函数，供任意组件在不持有 TimingMiddleware
    引用时直接使用。若当前无活跃 trace，直接 yield 并跳过记录。

    span 退出后，可通过 ``attributes`` 字典追加运行时数据（如工具输出）——
    在 ``with`` 块内直接修改传入的 ``attributes`` 字典即可，因为 finally
    中写入的是同一个引用。

    Args:
        span_type: span 类型，如 "intent" / "dag" / "dag.task" / "report" / "case_store"。
        name: span 名称（可读描述）。
        **attributes: 写入 span attributes 的键值对（须为 JSON 可序列化类型）。

    Example::

        with record_span("intent", "route_decision", route_level=1):
            # do routing ...
            pass
    """
    ctx = _current_trace.get()
    if ctx is None:
        yield attributes
        return

    span_id = str(uuid.uuid4())
    started_at = now_cn()
    t0 = time.monotonic()
    status = "ok"
    error: str | None = None
    _publish_span_start(span_type, name)
    try:
        yield attributes
    except BaseException as exc:
        status = "error"
        error = format_error_chain(exc)
        raise
    finally:
        duration_ms = (time.monotonic() - t0) * 1000
        sp = SpanEvent(
            trace_id=ctx.trace_id,
            span_id=span_id,
            parent_span_id=None,
            span_type=span_type,
            name=name,
            status=status,
            started_at=started_at,
            finished_at=now_cn(),
            duration_ms=round(duration_ms, 3),
            attributes=_safe_jsonable(attributes),
            error=error,
        )
        ctx.spans.append(sp)
        if span_type == "model":
            ctx.model_count += 1
        elif span_type == "tool":
            ctx.tool_count += 1
        _publish_span_end(
            span_type, name,
            duration_ms=round(duration_ms, 3),
            status=status,
        )
        active_store = ctx.store or get_trace_store()
        if active_store is not None:
            active_store.enqueue(sp)
        # 与 _record_span 对称：通过 record_span 上下文管理器记录的调用
        # （如 DAG executor 的 tool / dag.task）同样享受高频调用 INFO。
        if status == "ok" and _verbose_calls() and span_type in ("model", "tool"):
            attrs = attributes or {}
            if span_type == "model":
                out = attrs.get("output") if isinstance(attrs.get("output"), dict) else {}
                usage = out.get("usage") or {} if isinstance(out, dict) else {}
                pt = usage.get("input_tokens") or usage.get("prompt_tokens")
                ct = usage.get("output_tokens") or usage.get("completion_tokens")
                _logger.info(
                    "model call ok: name=%s duration=%.1fms "
                    "prompt_tokens=%s completion_tokens=%s",
                    name, duration_ms, pt, ct,
                )
            else:
                _logger.info(
                    "tool call ok: name=%s duration=%.1fms args=%s output=%s",
                    name, duration_ms,
                    _truncate_text(attrs.get("args") or attrs.get("input"), 120),
                    _truncate_text(attrs.get("output"), 120),
                )


def _safe_jsonable(value: Any) -> Any:
    """把任意对象转成 JSON 可序列化结构（用于 attributes JSON 列）。"""
    import json

    try:
        json.dumps(value, ensure_ascii=False, default=str)
        return value
    except Exception as exc:
        _logger.info("_safe_jsonable first-pass failed: %s", exc)
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception as exc2:
            _logger.info("_safe_jsonable second-pass failed, falling back to str(): %s", exc2)
            return str(value)
