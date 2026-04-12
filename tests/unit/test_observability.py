"""TimingMiddleware + TraceStore 单元测试。"""

from __future__ import annotations

import time

import pytest

import core.observability  # noqa: F401  触发 tables 注册到 Base.metadata
from core.observability.middleware import (
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
        _FakeToolCallRequest("query_purchase_orders", {"supplier_id": "SUP-001"}),
        tool_handler,
    )
    assert result == "tool-result"

    mw.finish_run(status="success")

    _wait_flush(
        store,
        lambda: len(store.list_runs(limit=10)) == 1
        and store.list_runs(limit=1)[0].status == "success",
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
    assert tool_span.name == "query_purchase_orders"
    assert tool_span.attributes["args"] == {"supplier_id": "SUP-001"}


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

    mw.finish_run(status="success")

    _wait_flush(store, lambda: len(store.list_runs(limit=10)) == 1)
    run = store.list_runs(limit=1)[0]
    assert run.status == "success"

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
    from core.observability.middleware import record_span

    with record_span("intent", "test_route") as attrs:
        attrs["level"] = 1
    # 不应抛异常


def test_record_span_emits_span(store):
    """record_span 在活跃 trace 中应写出指定类型的 span。"""
    from core.observability.middleware import record_span

    mw = TimingMiddleware(agent_name="span_agent", store=store, print_console=False)
    mw.start_run(session_id="s1", user_id="u1")

    with record_span("intent", "route_decision") as attrs:
        attrs["route_level"] = 1
        attrs["confidence"] = 0.85
        attrs["analysis_type"] = "three_way_match"
        time.sleep(0.01)

    mw.finish_run(status="success")

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
    from core.observability.middleware import record_span

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
    from core.observability.middleware import record_span

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
    from core.observability.middleware import record_span

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
    from core.observability.middleware import record_span

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
    from core.observability.middleware import record_span

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
