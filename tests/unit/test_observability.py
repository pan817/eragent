"""TimingMiddleware + TraceStore 单元测试。"""

from __future__ import annotations

import time

import pytest

import core.observability  # noqa: F401  触发 tables 注册到 Base.metadata
from core.observability.tracing import (
    TimingMiddleware,
    estimate_tokens,
    record_memory_span,
)
from core.observability.store import RunEvent, SpanEvent, TraceStore
from core.database import Base
from core.database.engine import create_engine_from_dsn, get_session_factory


@pytest.fixture()
def store(tmp_path):
    # 使用文件型 sqlite，确保后台线程与主线程共享同一份数据
    dsn = f"sqlite:///{tmp_path / 'trace.db'}"
    engine = create_engine_from_dsn(dsn)
    Base.metadata.create_all(engine)
    sf = get_session_factory(engine)
    s = TraceStore(sf, batch_size=5, flush_interval=0.05)
    s.start()
    yield s
    s.close(timeout=2.0)
    engine.dispose()


def _wait_flush(store: TraceStore, predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


class _FakeTool:
    def __init__(self, name: str):
        self.name = name


class _FakeToolCallRequest:
    def __init__(self, name: str, args: dict, tool_id: str = "tc-1"):
        self.tool_call = {"name": name, "args": args, "id": tool_id}
        self.tool = _FakeTool(name)
        self.state = {}
        self.runtime = None


class _FakeMessage:
    def __init__(self, content: str = "", role: str = "user"):
        self.content = content
        self.type = role


class _FakeModel:
    model_name = "glm-4"


class _FakeSystemMessage:
    def __init__(self, content: str = "你是一位专业的分析专家"):
        self.content = content


class _FakeArgsSchema:
    @staticmethod
    def schema():
        return {"type": "object", "properties": {"query": {"type": "string"}}}


class _FakeToolDef:
    def __init__(self, name: str = "query_data", description: str = "查询数据工具"):
        self.name = name
        self.description = description
        self.args_schema = _FakeArgsSchema()


class _FakeModelRequest:
    def __init__(self, messages=None, system_message=None, tools=None):
        self.model = _FakeModel()
        self.messages = messages if messages is not None else [
            _FakeMessage("你好"),
            _FakeMessage("分析一下", "system"),
            _FakeMessage("结果", "assistant"),
        ]
        self.system_message = system_message
        self.tools = tools if tools is not None else [_FakeToolDef(), _FakeToolDef("run_check", "执行检查")]


def test_middleware_tool_and_model_spans(store):
    mw = TimingMiddleware(agent_name="unit_agent", store=store, print_console=False)
    mw.start_run(session_id="s1", user_id="u1")

    def model_handler(req):
        time.sleep(0.01)
        return "model-result"

    result = mw.wrap_model_call(_FakeModelRequest(), model_handler)
    assert result == "model-result"

    def tool_handler(req):
        time.sleep(0.01)
        return "tool-result"

    result = mw.wrap_tool_call(
        _FakeToolCallRequest("query_purchase_orders", {"vendor_id": "SUP-001"}),
        tool_handler,
    )
    assert result == "tool-result"

    mw.finish_run(status="ok")

    _wait_flush(
        store,
        lambda: len(store.list_runs(limit=10)) == 1
        and store.list_runs(limit=1)[0].status == "ok",
    )

    runs = store.list_runs(limit=10)
    assert len(runs) == 1
    run = runs[0]
    assert run.agent_name == "unit_agent"
    assert run.session_id == "s1"
    assert run.model_call_count == 1
    assert run.tool_call_count == 1
    assert run.duration_ms is not None and run.duration_ms > 0

    found = store.get_run(run.trace_id)
    assert found is not None
    _, spans = found
    types = sorted(s.span_type for s in spans)
    assert types == ["agent", "model", "tool"]
    tool_span = next(s for s in spans if s.span_type == "tool")
    assert tool_span.name == "pg:query_purchase_orders"
    assert tool_span.attributes["args"] == {"vendor_id": "SUP-001"}


def test_middleware_records_tool_error(store):
    mw = TimingMiddleware(agent_name="err_agent", store=store, print_console=False)
    mw.start_run()

    def bad_handler(req):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        mw.wrap_tool_call(_FakeToolCallRequest("bad_tool", {}), bad_handler)

    mw.finish_run(status="error", error="boom")

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    runs = store.list_runs(limit=10)
    _, spans = store.get_run(runs[0].trace_id)
    tool_span = next(s for s in spans if s.span_type == "tool")
    assert tool_span.status == "error"
    assert "RuntimeError" in (tool_span.error or "")


def test_record_memory_span_no_active_trace():
    """record_memory_span 在无活跃 trace 时应静默 no-op，不抛异常。"""
    with record_memory_span("memory.write", user_id="u1", memory_type="t"):
        pass  # 无 trace 时不应有任何副作用


def test_record_memory_span_emits_span(store):
    """record_memory_span 在活跃 trace 中应写出 span_type='memory' 的 span。"""
    mw = TimingMiddleware(agent_name="mem_agent", store=store, print_console=False)
    mw.start_run(session_id="s1", user_id="u1")

    with record_memory_span(
        "memory.read",
        user_id="u1",
        mode="hybrid",
        limit=5,
    ):
        time.sleep(0.01)

    with record_memory_span(
        "memory.write",
        user_id="u1",
        memory_type="analysis_conclusion",
        filter_result="written",
        has_vector=False,
    ):
        time.sleep(0.01)

    mw.finish_run(status="ok")

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    run = store.list_runs(limit=1)[0]
    assert run.status == "ok"

    _, spans = store.get_run(run.trace_id)
    types = sorted(s.span_type for s in spans)
    assert types == ["agent", "memory", "memory"]

    memory_spans = [s for s in spans if s.span_type == "memory"]
    names = {s.name for s in memory_spans}
    assert names == {"memory.read", "memory.write"}

    # agent span 应包含 memory_calls 汇总
    agent_span = next(s for s in spans if s.span_type == "agent")
    assert agent_span.attributes["memory_calls"] == 2


def test_record_memory_span_captures_error(store):
    """record_memory_span 应正确捕获并记录异常，同时重新抛出。"""
    mw = TimingMiddleware(agent_name="err_agent", store=store, print_console=False)
    mw.start_run()

    with pytest.raises(ValueError, match="simulated"):
        with record_memory_span("memory.write", user_id="u1"):
            raise ValueError("simulated write failure")

    mw.finish_run(status="error")

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    mem_span = next(s for s in spans if s.span_type == "memory")
    assert mem_span.status == "error"
    assert "ValueError" in (mem_span.error or "")


# ============================================================
# record_span（通用 span 记录）
# ============================================================


def test_record_span_no_active_trace():
    """record_span 在无活跃 trace 时应静默 no-op。"""
    from core.observability.tracing import record_span

    with record_span("intent", "test_route") as attrs:
        attrs["level"] = 1
    # 不应抛异常


def test_record_span_emits_span(store):
    """record_span 在活跃 trace 中应写出指定类型的 span。"""
    from core.observability.tracing import record_span

    mw = TimingMiddleware(agent_name="span_agent", store=store, print_console=False)
    mw.start_run(session_id="s1", user_id="u1")

    with record_span("intent", "route_decision") as attrs:
        attrs["route_level"] = 1
        attrs["confidence"] = 0.85
        attrs["analysis_type"] = "three_way_match"
        time.sleep(0.01)

    mw.finish_run(status="ok")

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) >= 1)
    runs = store.list_runs(limit=1)
    assert len(runs) == 1
    _, spans = store.get_run(runs[0].trace_id)
    intent_spans = [s for s in spans if s.span_type == "intent"]
    assert len(intent_spans) == 1
    sp = intent_spans[0]
    assert sp.name == "route_decision"
    assert sp.attributes["route_level"] == 1
    assert sp.attributes["confidence"] == 0.85
    assert sp.status == "ok"
    assert sp.duration_ms >= 0


def test_record_span_captures_error(store):
    """record_span 应正确捕获异常并记录 error 状态。"""
    from core.observability.tracing import record_span

    mw = TimingMiddleware(agent_name="err_agent", store=store, print_console=False)
    mw.start_run()

    with pytest.raises(RuntimeError, match="test error"):
        with record_span("dag", "test_dag"):
            raise RuntimeError("test error")

    mw.finish_run(status="error")

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) >= 1)
    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    dag_span = next(s for s in spans if s.span_type == "dag")
    assert dag_span.status == "error"
    assert "RuntimeError" in (dag_span.error or "")


def test_record_span_multiple_types(store):
    """同一个 trace 中可以有多种 span 类型。"""
    from core.observability.tracing import record_span

    mw = TimingMiddleware(agent_name="multi_agent", store=store, print_console=False)
    mw.start_run()

    with record_span("intent", "route"):
        time.sleep(0.01)
    with record_span("dag", "execute"):
        time.sleep(0.01)
    with record_span("dag.task", "t1:query_po"):
        time.sleep(0.01)
    with record_span("report", "generate"):
        time.sleep(0.01)

    mw.finish_run()

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) >= 1)
    # 等待所有 span flush 完成（4 个 record_span + 1 个 agent）
    def _all_spans_flushed():
        runs = store.list_runs(limit=1)
        if not runs:
            return False
        _, spans = store.get_run(runs[0].trace_id)
        return len(spans) >= 4
    _wait_flush(store, _all_spans_flushed)

    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    types = {s.span_type for s in spans}
    assert {"intent", "dag", "dag.task", "report"}.issubset(types)


def test_record_span_with_non_serializable_attrs(store):
    """record_span 应通过 _safe_jsonable 处理不可序列化的 attributes。"""
    from core.observability.tracing import record_span

    mw = TimingMiddleware(agent_name="jsonable_agent", store=store, print_console=False)
    mw.start_run()

    with record_span("test", "safe_jsonable") as attrs:
        attrs["normal"] = "text"
        attrs["number"] = 42
        attrs["nested"] = {"a": [1, 2, 3]}
        time.sleep(0.01)

    mw.finish_run()

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) >= 1)
    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    test_spans = [s for s in spans if s.span_type == "test"]
    assert len(test_spans) == 1
    assert test_spans[0].attributes["normal"] == "text"
    assert test_spans[0].attributes["number"] == 42


def test_store_enqueue_drops_on_full(tmp_path):
    engine = create_engine_from_dsn("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = get_session_factory(engine)
    s = TraceStore(sf, batch_size=1, flush_interval=0.05, queue_maxsize=1)
    # 不启动 worker，队列会满
    s.enqueue(RunEvent(kind="run_start", trace_id="t1", agent_name="a"))
    # 第二次不应抛异常
    s.enqueue(RunEvent(kind="run_start", trace_id="t2", agent_name="a"))
    engine.dispose()


# ============================================================
# estimate_tokens
# ============================================================


def test_estimate_tokens_empty():
    """空字符串或 None 应返回 0。"""
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_nonempty():
    """非空字符串应返回正整数。"""
    result = estimate_tokens("你好世界，这是一段测试文本")
    assert result > 0
    assert isinstance(result, int)


def test_estimate_tokens_short():
    """极短字符串至少返回 1。"""
    assert estimate_tokens("a") >= 1


# ============================================================
# model span estimated_input_tokens
# ============================================================


def test_model_span_has_estimated_input_tokens(store):
    """model span 应包含 estimated_input_tokens 属性。"""
    mw = TimingMiddleware(agent_name="token_agent", store=store, print_console=False)
    mw.start_run(session_id="s1")

    def model_handler(req):
        return "ok"

    mw.wrap_model_call(_FakeModelRequest(), model_handler)
    mw.finish_run()

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    model_span = next(s for s in spans if s.span_type == "model")
    assert "estimated_input_tokens" in model_span.attributes
    assert model_span.attributes["estimated_input_tokens"] > 0


def test_estimated_input_tokens_includes_system_message(store):
    """estimated_input_tokens 应包含 system_message 的 token。"""
    mw = TimingMiddleware(agent_name="sys_agent", store=store, print_console=False)
    mw.start_run()

    def handler(req):
        return "ok"

    # 无 system_message
    req_no_sys = _FakeModelRequest(
        messages=[_FakeMessage("短消息")],
        system_message=None,
        tools=[],
    )
    mw.wrap_model_call(req_no_sys, handler)

    # 有 system_message
    req_with_sys = _FakeModelRequest(
        messages=[_FakeMessage("短消息")],
        system_message=_FakeSystemMessage("这是一段很长的系统提示词" * 50),
        tools=[],
    )
    mw.wrap_model_call(req_with_sys, handler)

    mw.finish_run()

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    model_spans = [s for s in spans if s.span_type == "model"]
    assert len(model_spans) == 2
    tokens_no_sys = model_spans[0].attributes["estimated_input_tokens"]
    tokens_with_sys = model_spans[1].attributes["estimated_input_tokens"]
    assert tokens_with_sys > tokens_no_sys


def test_estimated_input_tokens_includes_tool_schemas(store):
    """estimated_input_tokens 应包含工具定义 schema 的 token。"""
    mw = TimingMiddleware(agent_name="tool_agent", store=store, print_console=False)
    mw.start_run()

    def handler(req):
        return "ok"

    # 无 tools
    req_no_tools = _FakeModelRequest(
        messages=[_FakeMessage("查询")],
        tools=[],
    )
    mw.wrap_model_call(req_no_tools, handler)

    # 有 tools
    req_with_tools = _FakeModelRequest(
        messages=[_FakeMessage("查询")],
        tools=[_FakeToolDef(f"tool_{i}", f"工具描述 {i}" * 20) for i in range(8)],
    )
    mw.wrap_model_call(req_with_tools, handler)

    mw.finish_run()

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    model_spans = [s for s in spans if s.span_type == "model"]
    tokens_no_tools = model_spans[0].attributes["estimated_input_tokens"]
    tokens_with_tools = model_spans[1].attributes["estimated_input_tokens"]
    assert tokens_with_tools > tokens_no_tools


# ============================================================
# agent span token_summary
# ============================================================


def test_agent_span_has_token_summary(store):
    """finish_run 后 agent span 应包含 token_summary 汇总。"""
    mw = TimingMiddleware(agent_name="summary_agent", store=store, print_console=False)
    mw.start_run()

    def model_handler(req):
        return "result"

    mw.wrap_model_call(_FakeModelRequest(), model_handler)
    mw.finish_run()

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    agent_span = next(s for s in spans if s.span_type == "agent")
    ts = agent_span.attributes.get("token_summary")
    assert ts is not None
    assert "total_prompt_tokens" in ts
    assert "total_completion_tokens" in ts
    assert "peak_prompt_tokens" in ts


# ============================================================
# store.get_token_summary
# ============================================================


def test_get_token_summary_returns_none_for_missing_trace(store):
    """不存在的 trace_id 应返回 None。"""
    assert store.get_token_summary("nonexistent") is None


def test_get_token_summary_extracts_from_agent_span(store):
    """应从 agent span 的 attributes 中提取 token_summary。"""
    mw = TimingMiddleware(agent_name="ts_agent", store=store, print_console=False)
    trace_id = mw.start_run()

    def model_handler(req):
        return "ok"

    mw.wrap_model_call(_FakeModelRequest(), model_handler)
    mw.finish_run()

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    ts = store.get_token_summary(trace_id)
    assert ts is not None
    assert isinstance(ts, dict)
    assert "total_prompt_tokens" in ts


# ============================================================
# finish_run token_summary: usage fallback（ReportAgent 风格 span）
# ============================================================


def test_token_summary_with_toplevel_usage(store):
    """ReportAgent 风格的 model span（usage 在 attrs 顶层而非 output 内）
    也应被 finish_run 正确汇总到 token_summary。"""
    from core.observability.tracing import record_span

    mw = TimingMiddleware(agent_name="fallback_agent", store=store, print_console=False)
    mw.start_run()

    # 模拟 ReportAgent 手动记录的 model span：output 是 string，usage 在顶层
    with record_span("model", "qwen3-max") as attrs:
        attrs["model"] = "qwen3-max"
        attrs["input"] = "some prompt"
        attrs["output"] = "report content"  # string, not dict
        attrs["usage"] = {"input_tokens": 500, "output_tokens": 200}

    mw.finish_run()

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    agent_span = next(s for s in spans if s.span_type == "agent")
    ts = agent_span.attributes["token_summary"]
    assert ts["total_prompt_tokens"] == 500
    assert ts["total_completion_tokens"] == 200
    assert ts["peak_prompt_tokens"] == 500


def test_token_summary_mixed_span_styles(store):
    """同一 trace 中混合 TimingMiddleware 和 ReportAgent 风格的 model span，
    token 应全部被累加。"""
    from core.observability.tracing import record_span

    mw = TimingMiddleware(agent_name="mixed_agent", store=store, print_console=False)
    mw.start_run()

    # 模拟 TimingMiddleware 风格：output 是 dict，内含 usage
    with record_span("model", "mw_model") as attrs:
        attrs["output"] = {
            "content": "...",
            "usage": {"prompt_tokens": 1000, "completion_tokens": 300},
        }

    # 模拟 ReportAgent 风格：output 是 string，usage 在顶层
    with record_span("model", "report_model") as attrs:
        attrs["output"] = "report text"
        attrs["usage"] = {"input_tokens": 800, "output_tokens": 150}

    mw.finish_run()

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    agent_span = next(s for s in spans if s.span_type == "agent")
    ts = agent_span.attributes["token_summary"]
    assert ts["total_prompt_tokens"] == 1800   # 1000 + 800
    assert ts["total_completion_tokens"] == 450  # 300 + 150
    assert ts["peak_prompt_tokens"] == 1000


# ============================================================
# record_span increments model_count / tool_count
# ============================================================


def test_record_span_increments_model_and_tool_counts(store):
    """record_span("model"/"tool") should increment ctx counters
    so that finish_run emits correct model_call_count / tool_call_count."""
    from core.observability.tracing import record_span

    mw = TimingMiddleware(agent_name="count_agent", store=store, print_console=False)
    mw.start_run()

    # 2 tool spans (simulating DAG executor)
    with record_span("tool", "query_vendor_master") as attrs:
        attrs["tool"] = "query_vendor_master"
    with record_span("tool", "calculate_supplier_kpis") as attrs:
        attrs["tool"] = "calculate_supplier_kpis"

    # 1 model span (simulating ReportAgent)
    with record_span("model", "qwen3-max") as attrs:
        attrs["model"] = "qwen3-max"
        attrs["output"] = "report"

    mw.finish_run()

    # Wait until run_end has been flushed (status changes from "running")
    _wait_flush(
        store,
        lambda: (
            store.list_runs(limit=1)
            and store.list_runs(limit=1)[0].status != "running"
        ),
    )
    run = store.list_runs(limit=1)[0]
    assert run.model_call_count == 1
    assert run.tool_call_count == 2

    _, spans = store.get_run(run.trace_id)
    agent_span = next(s for s in spans if s.span_type == "agent")
    assert agent_span.attributes["model_calls"] == 1
    assert agent_span.attributes["tool_calls"] == 2


# ============================================================
# _classify_llm_error
# ============================================================


def test_classify_llm_error_timeout():
    from core.observability.tracing import _classify_llm_error

    assert _classify_llm_error(TimeoutError("timed out")) == "timeout"
    assert _classify_llm_error(OSError("connect timeout")) == "timeout"


def test_classify_llm_error_timeout_in_message():
    from core.observability.tracing import _classify_llm_error

    assert _classify_llm_error(RuntimeError("request timed out")) == "timeout"


def test_classify_llm_error_connection():
    from core.observability.tracing import _classify_llm_error

    assert _classify_llm_error(RuntimeError("Connection refused")) == "connection_error"


def test_classify_llm_error_rate_limit():
    from core.observability.tracing import _classify_llm_error

    assert _classify_llm_error(RuntimeError("Rate limit exceeded")) == "rate_limit"
    assert _classify_llm_error(RuntimeError("Error 429: too many requests")) == "rate_limit"


def test_classify_llm_error_auth():
    from core.observability.tracing import _classify_llm_error

    assert _classify_llm_error(RuntimeError("Authentication failed")) == "auth_error"
    assert _classify_llm_error(RuntimeError("Error 401")) == "auth_error"


def test_classify_llm_error_server():
    from core.observability.tracing import _classify_llm_error

    assert _classify_llm_error(RuntimeError("HTTP 500 Internal Server Error")) == "server_error"
    assert _classify_llm_error(RuntimeError("502 Bad Gateway")) == "server_error"
    assert _classify_llm_error(RuntimeError("503 Service Unavailable")) == "server_error"


def test_classify_llm_error_generic():
    from core.observability.tracing import _classify_llm_error

    assert _classify_llm_error(ValueError("bad value")) == "ValueError"


# ============================================================
# console.py format_io_panel
# ============================================================


def test_format_io_panel_model():
    from core.observability.console import format_io_panel

    span = SpanEvent(
        trace_id="t1", span_id="s1", parent_span_id=None,
        span_type="model", name="qwen3-max", status="ok",
        started_at=None, finished_at=None, duration_ms=100.0,
        attributes={
            "input": [{"role": "user", "content": "你好"}],
            "output": {"content": "回复内容", "tool_calls": None, "usage": {"tokens": 10}},
        },
        error=None,
    )
    result = format_io_panel(span)
    assert "model" in result
    assert "qwen3-max" in result
    assert "你好" in result
    assert "回复内容" in result


def test_format_io_panel_tool():
    from core.observability.console import format_io_panel

    span = SpanEvent(
        trace_id="t1", span_id="s1", parent_span_id=None,
        span_type="tool", name="query_data", status="ok",
        started_at=None, finished_at=None, duration_ms=50.0,
        attributes={"input": "args here", "output": "result here"},
        error=None,
    )
    result = format_io_panel(span)
    assert "tool" in result
    assert "query_data" in result
    assert "args here" in result
    assert "result here" in result


def test_format_io_panel_with_error():
    from core.observability.console import format_io_panel

    span = SpanEvent(
        trace_id="t1", span_id="s1", parent_span_id=None,
        span_type="model", name="test", status="error",
        started_at=None, finished_at=None, duration_ms=10.0,
        attributes={"input": [], "output": {}},
        error="TimeoutError: timed out",
    )
    result = format_io_panel(span)
    assert "error" in result.lower()
    assert "TimeoutError" in result


def test_format_io_panel_non_io_span():
    from core.observability.console import format_io_panel

    span = SpanEvent(
        trace_id="t1", span_id="s1", parent_span_id=None,
        span_type="memory", name="memory.read", status="ok",
        started_at=None, finished_at=None, duration_ms=5.0,
        attributes={}, error=None,
    )
    assert format_io_panel(span) == ""


def test_format_io_panel_model_string_output():
    """model output 为非 dict 时也能正常输出。"""
    from core.observability.console import format_io_panel

    span = SpanEvent(
        trace_id="t1", span_id="s1", parent_span_id=None,
        span_type="model", name="test", status="ok",
        started_at=None, finished_at=None, duration_ms=10.0,
        attributes={"input": [], "output": "raw string output"},
        error=None,
    )
    result = format_io_panel(span)
    assert "raw string output" in result


def test_format_io_panel_model_with_tool_calls():
    """model output 包含 tool_calls 时应输出。"""
    from core.observability.console import format_io_panel

    span = SpanEvent(
        trace_id="t1", span_id="s1", parent_span_id=None,
        span_type="model", name="test", status="ok",
        started_at=None, finished_at=None, duration_ms=10.0,
        attributes={
            "input": [],
            "output": {
                "content": "",
                "tool_calls": [{"name": "query_data", "args": {}}],
                "usage": {"tokens": 5},
            },
        },
        error=None,
    )
    result = format_io_panel(span)
    assert "tool_calls" in result
    assert "usage" in result


# ============================================================
# middleware model span error_type on exception
# ============================================================


def test_model_span_error_type_on_exception(store):
    """LLM 调用异常时 span 应包含 error_type 和 elapsed_ms。"""
    mw = TimingMiddleware(agent_name="err_agent", store=store, print_console=False)
    mw.start_run()

    def bad_model_handler(req):
        raise TimeoutError("request timed out")

    with pytest.raises(TimeoutError):
        mw.wrap_model_call(_FakeModelRequest(), bad_model_handler)

    mw.finish_run(status="error", error="timeout")

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    model_span = next(s for s in spans if s.span_type == "model")
    assert model_span.status == "error"
    assert model_span.attributes.get("error_type") == "timeout"
    assert model_span.attributes.get("elapsed_ms") is not None
    assert model_span.attributes["elapsed_ms"] >= 0


# ============================================================
# console.py format_tree / format_summary
# ============================================================


def test_format_tree():
    from core.observability.console import format_tree
    from core.time_utils import now_cn

    now = now_cn()
    spans = [
        SpanEvent(
            trace_id="t1", span_id="root", parent_span_id=None,
            span_type="agent", name="p2p_agent", status="ok",
            started_at=now, finished_at=now, duration_ms=100.0,
            attributes={}, error=None,
        ),
        SpanEvent(
            trace_id="t1", span_id="s1", parent_span_id="root",
            span_type="tool", name="query_data", status="ok",
            started_at=now, finished_at=now, duration_ms=50.0,
            attributes={"args": {"days": 30}}, error=None,
        ),
        SpanEvent(
            trace_id="t1", span_id="s2", parent_span_id="root",
            span_type="model", name="qwen3-max", status="error",
            started_at=now, finished_at=now, duration_ms=200.0,
            attributes={"message_count": 5}, error="timeout",
        ),
    ]
    result = format_tree(spans, "t1")
    assert "trace t1" in result
    assert "query_data" in result
    assert "qwen3-max" in result
    assert "[error]" in result
    assert "args=" in result
    assert "msgs=5" in result


def test_format_summary():
    from core.observability.console import format_summary
    from core.time_utils import now_cn

    now = now_cn()
    spans = [
        SpanEvent(
            trace_id="t1", span_id="s1", parent_span_id=None,
            span_type="model", name="m1", status="ok",
            started_at=now, finished_at=now, duration_ms=100.0,
            attributes={}, error=None,
        ),
        SpanEvent(
            trace_id="t1", span_id="s2", parent_span_id=None,
            span_type="tool", name="t1", status="ok",
            started_at=now, finished_at=now, duration_ms=50.0,
            attributes={}, error=None,
        ),
    ]
    result = format_summary(spans, 200.0)
    assert "model=1" in result
    assert "tool=1" in result
    assert "memory=0" in result


# ============================================================
# format_error_chain：完整异常链 + 8KB 上限截断
# ============================================================


def test_format_error_chain_captures_cause():
    """wrapped exception 的 __cause__ 根因必须出现在 error 文本中，
    不应像旧实现那样只保留最外层 str(exc)。"""
    from core.observability.tracing import format_error_chain

    try:
        try:
            raise ValueError("root_cause_marker")
        except ValueError as inner:
            raise RuntimeError("outer_wrapper_marker") from inner
    except RuntimeError as exc:
        text = format_error_chain(exc)

    assert "root_cause_marker" in text
    assert "outer_wrapper_marker" in text
    # format_exception(chain=True) 会在两段 traceback 之间插入衔接句
    assert "direct cause" in text or "another exception" in text


def test_format_error_chain_preserves_full_stack():
    """栈深 > 3 的场景，完整栈都应保留，不被 limit=3 截断。"""
    from core.observability.tracing import format_error_chain

    def level_a():
        level_b()

    def level_b():
        level_c()

    def level_c():
        level_d()

    def level_d():
        raise RuntimeError("deep_error")

    try:
        level_a()
    except RuntimeError as exc:
        text = format_error_chain(exc)

    # 4 层业务栈，应当都在栈里
    for fname in ("level_a", "level_b", "level_c", "level_d"):
        assert fname in text, f"frame {fname} missing from traceback"


def test_format_error_chain_head_tail_truncation():
    """超过 max_len 时采用头尾截断，保留链顶根因 + 链尾外层异常 + 标记。"""
    from core.observability.tracing import format_error_chain

    # ROOT 异常消息填大量字符，让完整异常链远超 max_len 触发头尾截断；
    # OUTER 消息保持简短，模拟真实 LLM 错误链（深栈 + 短外层消息）。
    bulky_root_msg = "ROOT_MARKER_" + ("z" * 8000)
    try:
        try:
            raise ValueError(bulky_root_msg)
        except ValueError as inner:
            raise RuntimeError("OUTER_MARKER_short") from inner
    except RuntimeError as exc:
        full = format_error_chain(exc, max_len=100_000)
        text = format_error_chain(exc, max_len=2000)

    assert len(full) > 2000, "test precondition: full text must exceed cap"
    assert len(text) <= 2000
    assert "truncated" in text and "full length=" in text
    # 头段覆盖 ROOT 异常类型与 marker 前缀（ValueError: ROOT_MARKER_...）
    assert "ROOT_MARKER_" in text
    # 尾段覆盖 OUTER 异常完整行（RuntimeError: OUTER_MARKER_short）
    assert "OUTER_MARKER_short" in text


def test_format_error_chain_no_truncation_when_short():
    """短异常不应被截断，也不带 marker。"""
    from core.observability.tracing import format_error_chain

    try:
        raise ValueError("short")
    except ValueError as exc:
        text = format_error_chain(exc, max_len=8192)

    assert "truncated" not in text
    assert text.rstrip().endswith("ValueError: short")


def test_middleware_tool_error_includes_full_chain(store):
    """TimingMiddleware 通过 format_error_chain 捕获的 tool 异常应包含异常链。"""
    mw = TimingMiddleware(agent_name="err_agent", store=store, print_console=False)
    mw.start_run()

    def bad_handler(req):
        try:
            raise ValueError("db_root_cause")
        except ValueError as inner:
            raise RuntimeError("tool_wrapper") from inner

    with pytest.raises(RuntimeError):
        mw.wrap_tool_call(_FakeToolCallRequest("bad_tool", {}), bad_handler)

    mw.finish_run(status="error")

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    _, spans = store.get_run(store.list_runs(limit=1)[0].trace_id)
    tool_span = next(s for s in spans if s.span_type == "tool")
    assert tool_span.status == "error"
    err = tool_span.error or ""
    # 根因与外层异常都要在 error 字段中
    assert "db_root_cause" in err
    assert "RuntimeError" in err
    assert "tool_wrapper" in err


# ---------------------------------------------------------------------------
# flush_now_sync 同步屏障（修复 SSE done / 快照 running 的 race）
# ---------------------------------------------------------------------------


import threading  # noqa: E402

from core.time_utils import now_cn  # noqa: E402


def _make_run_end(trace_id: str, *, status: str = "success") -> RunEvent:
    now = now_cn()
    return RunEvent(
        kind="run_end",
        trace_id=trace_id,
        agent_name="p2p",
        session_id="s1",
        user_id="u1",
        started_at=now,
        finished_at=now,
        duration_ms=1.0,
        status=status,
    )


def test_flush_now_sync_returns_true_after_run_end_flushed(store: TraceStore) -> None:
    """正常路径：enqueue run_end → flush_now_sync 在 worker 线程 commit 后立即返回 True。"""
    trace_id = "trace-a"
    # 先写 run_start（否则 run_end 是对不存在行的 UPDATE，apply 里会走 INSERT 分支也行）
    store.enqueue(RunEvent(
        kind="run_start",
        trace_id=trace_id,
        agent_name="p2p",
        session_id="s1",
        user_id="u1",
        started_at=now_cn(),
    ))
    store.enqueue(_make_run_end(trace_id, status="success"))

    ok = store.flush_now_sync(trace_id, timeout=3.0)
    assert ok is True

    # 屏障返回后 DB 应当已终态
    run = store.list_runs(limit=1)[0]
    assert run.trace_id == trace_id
    assert run.status == "success"
    assert run.finished_at is not None


def test_flush_now_sync_fast_path_when_flush_preceeds_wait(
    store: TraceStore,
) -> None:
    """flush 先于 wait：调用 flush_now_sync 时 _run_end_flushed 里已经记下，
    不用注册 waiter，立即返回 True。"""
    trace_id = "trace-fast"
    store.enqueue(RunEvent(
        kind="run_start",
        trace_id=trace_id,
        agent_name="p2p",
        session_id="s1",
        user_id="u1",
        started_at=now_cn(),
    ))
    store.enqueue(_make_run_end(trace_id))

    # 给后台线程充分时间完成 flush + signal
    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)

    # 现在才来等 —— 应走快路径
    t0 = time.monotonic()
    ok = store.flush_now_sync(trace_id, timeout=3.0)
    assert ok is True
    # 快路径必须是几乎零等待（给 10ms 余量）
    assert time.monotonic() - t0 < 0.1


def test_flush_now_sync_times_out_when_no_run_end(store: TraceStore) -> None:
    """run_end 从未入队：flush_now_sync 必须按 timeout 超时返回 False，
    而不是永远阻塞把 registry finally 吊死。"""
    t0 = time.monotonic()
    ok = store.flush_now_sync("never-enqueued", timeout=0.3)
    elapsed = time.monotonic() - t0
    assert ok is False
    assert 0.25 < elapsed < 1.0


def test_flush_now_sync_concurrent_waiters_independent(store: TraceStore) -> None:
    """不同 trace_id 的 wait 相互独立，不会因为一个 trace 先 flush 把另一个也唤醒。"""
    trace_a, trace_b = "trace-ind-a", "trace-ind-b"
    for tid in (trace_a, trace_b):
        store.enqueue(RunEvent(
            kind="run_start",
            trace_id=tid,
            agent_name="p2p",
            session_id="s1",
            user_id="u1",
            started_at=now_cn(),
        ))
    # 只给 trace_a 入 run_end
    store.enqueue(_make_run_end(trace_a))

    result_a: list[bool] = []
    result_b: list[bool] = []

    def wait_a() -> None:
        result_a.append(store.flush_now_sync(trace_a, timeout=2.0))

    def wait_b() -> None:
        result_b.append(store.flush_now_sync(trace_b, timeout=0.3))

    ta = threading.Thread(target=wait_a)
    tb = threading.Thread(target=wait_b)
    ta.start()
    tb.start()
    ta.join(timeout=3.0)
    tb.join(timeout=3.0)

    assert result_a == [True], "trace_a 有 run_end 必须被唤醒"
    assert result_b == [False], "trace_b 无 run_end 必须 timeout，不被 a 顺带唤醒"


def test_flush_now_sync_signals_even_if_commit_fails(tmp_path) -> None:
    """flush 内部 commit 异常也要唤醒 waiter，避免调用方永远挂在 wait 上。"""
    dsn = f"sqlite:///{tmp_path / 'trace_err.db'}"
    engine = create_engine_from_dsn(dsn)
    Base.metadata.create_all(engine)
    sf = get_session_factory(engine)

    store = TraceStore(sf, batch_size=1, flush_interval=0.05)
    store.start()
    try:
        # 手工把 _apply 打坏，让 _flush 的 session.commit 抛异常
        original_apply = store._apply  # noqa: SLF001

        def broken_apply(session, ev):
            if isinstance(ev, RunEvent) and ev.kind == "run_end":
                raise RuntimeError("simulated DB error")
            return original_apply(session, ev)

        store._apply = broken_apply  # type: ignore[attr-defined]

        trace_id = "trace-err"
        store.enqueue(RunEvent(
            kind="run_start",
            trace_id=trace_id,
            agent_name="p2p",
            session_id="s1",
            user_id="u1",
            started_at=now_cn(),
        ))
        store.enqueue(_make_run_end(trace_id))

        # 即使 commit 失败，waiter 也必须被 signal（返回 True）
        # 这样 registry 才能继续 publish_done
        ok = store.flush_now_sync(trace_id, timeout=2.0)
        assert ok is True
    finally:
        store.close(timeout=2.0)
        engine.dispose()
