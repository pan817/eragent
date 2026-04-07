"""TimingMiddleware + TraceStore 单元测试。"""

from __future__ import annotations

import time

import pytest

# 确保 trace 模型注册到 Base.metadata
from core.observability import models as _trace_models  # noqa: F401
from core.observability.middleware import TimingMiddleware
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


class _FakeModel:
    model_name = "glm-4"


class _FakeModelRequest:
    def __init__(self):
        self.model = _FakeModel()
        self.messages = [1, 2, 3]
        self.tools = [object(), object()]


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
