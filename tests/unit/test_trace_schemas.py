"""api/schemas/trace.py 单元测试。"""

from __future__ import annotations

import pytest

from api.schemas.trace import IoSpanOut, RunDetailOut, RunOut, SpanOut, StatRow
from core.time_utils import now_cn


NOW = now_cn()


class TestSpanOut:
    def test_basic(self) -> None:
        sp = SpanOut(
            span_id="sp1",
            trace_id="tr1",
            parent_span_id=None,
            span_type="model",
            name="llm_call",
            status="ok",
            started_at=NOW,
            finished_at=NOW,
            duration_ms=12.3,
        )
        assert sp.span_id == "sp1"
        assert sp.parent_span_id is None
        assert sp.attributes is None
        assert sp.error is None

    def test_with_attributes_and_error(self) -> None:
        sp = SpanOut(
            span_id="sp2",
            trace_id="tr1",
            parent_span_id="sp0",
            span_type="tool",
            name="tool_call",
            status="error",
            started_at=NOW,
            finished_at=None,
            duration_ms=None,
            attributes={"key": "val"},
            error="timeout",
        )
        assert sp.attributes == {"key": "val"}
        assert sp.error == "timeout"
        assert sp.finished_at is None

    def test_from_attributes(self) -> None:
        """from_attributes=True 支持从 ORM-like 对象构造。"""

        class _FakeSpan:
            span_id = "s1"
            trace_id = "t1"
            parent_span_id = None
            span_type = "checkpoint"
            name = "get_tuple"
            status = "ok"
            started_at = NOW
            finished_at = NOW
            duration_ms = 5.0
            attributes = None
            error = None

        sp = SpanOut.model_validate(_FakeSpan())
        assert sp.name == "get_tuple"


class TestRunOut:
    def test_basic(self) -> None:
        r = RunOut(
            trace_id="tr1",
            agent_name="p2p_agent",
            session_id="sess1",
            user_id="u1",
            status="ok",
            started_at=NOW,
            finished_at=NOW,
            duration_ms=100.0,
            model_call_count=2,
            tool_call_count=3,
        )
        assert r.agent_name == "p2p_agent"
        assert r.error is None

    def test_null_optional_fields(self) -> None:
        r = RunOut(
            trace_id="tr2",
            agent_name="agent",
            session_id=None,
            user_id=None,
            status="failed",
            started_at=NOW,
            finished_at=None,
            duration_ms=None,
            model_call_count=0,
            tool_call_count=0,
            error="crash",
        )
        assert r.session_id is None
        assert r.user_id is None
        assert r.error == "crash"
        assert r.token_summary is None

    def test_with_token_summary(self) -> None:
        ts = {
            "total_prompt_tokens": 5000,
            "total_completion_tokens": 1000,
            "peak_prompt_tokens": 3000,
            "context_budget": {
                "system_prompt_tokens": 800,
                "long_term_memory_tokens": 500,
                "short_term_memory_tokens": 200,
            },
        }
        r = RunOut(
            trace_id="tr3",
            agent_name="agent",
            session_id="s1",
            user_id="u1",
            status="ok",
            started_at=NOW,
            finished_at=NOW,
            duration_ms=100.0,
            model_call_count=2,
            tool_call_count=1,
            token_summary=ts,
        )
        assert r.token_summary is not None
        assert r.token_summary["total_prompt_tokens"] == 5000
        assert "context_budget" in r.token_summary


class TestRunDetailOut:
    def test_empty_spans(self) -> None:
        r = RunDetailOut(
            trace_id="tr3",
            agent_name="agent",
            session_id="s",
            user_id="u",
            status="ok",
            started_at=NOW,
            finished_at=NOW,
            duration_ms=50.0,
            model_call_count=1,
            tool_call_count=0,
        )
        assert r.spans == []

    def test_with_spans(self) -> None:
        sp = SpanOut(
            span_id="sp1",
            trace_id="tr3",
            parent_span_id=None,
            span_type="model",
            name="llm",
            status="ok",
            started_at=NOW,
            finished_at=NOW,
            duration_ms=20.0,
        )
        r = RunDetailOut(
            trace_id="tr3",
            agent_name="agent",
            session_id="s",
            user_id="u",
            status="ok",
            started_at=NOW,
            finished_at=NOW,
            duration_ms=50.0,
            model_call_count=1,
            tool_call_count=0,
            spans=[sp],
        )
        assert len(r.spans) == 1
        assert r.spans[0].span_id == "sp1"


class TestIoSpanOut:
    def test_basic(self) -> None:
        io = IoSpanOut(
            span_id="sp1",
            trace_id="tr1",
            span_type="model",
            name="llm",
            status="ok",
            started_at=NOW,
            duration_ms=15.0,
            input=[{"role": "user", "content": "hello"}],
            output={"content": "hi"},
        )
        assert io.input[0]["role"] == "user"
        assert io.output["content"] == "hi"
        assert io.error is None

    def test_none_fields(self) -> None:
        io = IoSpanOut(
            span_id="sp2",
            trace_id="tr1",
            span_type="tool",
            name="tool",
            status="ok",
            started_at=NOW,
            duration_ms=None,
        )
        assert io.input is None
        assert io.output is None


class TestStatRow:
    def test_basic(self) -> None:
        row = StatRow(
            key="three_way_match",
            span_type="tool",
            count=5,
            avg_ms=100.0,
            p50_ms=90.0,
            p95_ms=200.0,
            min_ms=50.0,
            max_ms=300.0,
        )
        assert row.key == "three_way_match"
        assert row.count == 5
