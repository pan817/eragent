"""core/observability/checkpointer.py 单元测试。

测试 attach_tracing 给 saver 打补丁后，各方法能正确记录 span，
以及异常情况下 status="error" 且异常被重新抛出。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.observability.checkpointer import (
    _checkpoint_id,
    _summarize_checkpoint,
    _summarize_tuple,
    _thread_id,
    _truncate,
    attach_tracing,
)
from core.observability.middleware import TimingMiddleware
from core.observability.store import SpanEvent


# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------


class TestTruncate:
    def test_short_passthrough(self):
        assert _truncate("hello", 10) == "hello"

    def test_long_truncated(self):
        result = _truncate("a" * 600, 500)
        assert result.endswith("…")
        assert len(result) == 501

    def test_exact_limit(self):
        s = "x" * 500
        assert _truncate(s, 500) == s


class TestThreadId:
    def test_extracts_thread_id(self):
        config = {"configurable": {"thread_id": "t123"}}
        assert _thread_id(config) == "t123"

    def test_missing_returns_none(self):
        assert _thread_id({}) is None
        assert _thread_id(None) is None

    def test_invalid_structure(self):
        assert _thread_id("bad") is None


class TestCheckpointId:
    def test_extracts_checkpoint_id(self):
        config = {"configurable": {"checkpoint_id": "cp1"}}
        assert _checkpoint_id(config) == "cp1"

    def test_missing_returns_none(self):
        assert _checkpoint_id({}) is None


class TestSummarizeTuple:
    def test_none_returns_no_hit(self):
        result = _summarize_tuple(None)
        assert result == {"hit": False}

    def test_valid_tuple(self):
        tup = MagicMock()
        tup.checkpoint = {
            "id": "cp1",
            "ts": "2026-04-10T00:00:00Z",
            "channel_values": {"messages": [1, 2, 3]},
        }
        tup.metadata = {"step": 2, "source": "loop"}
        result = _summarize_tuple(tup)
        assert result["hit"] is True
        assert result["n_messages"] == 3
        assert result["step"] == 2
        assert result["source"] == "loop"

    def test_no_messages_key(self):
        tup = MagicMock()
        tup.checkpoint = {"id": "cp1", "channel_values": {}}
        tup.metadata = {}
        result = _summarize_tuple(tup)
        assert result["hit"] is True
        assert "n_messages" not in result


class TestSummarizeCheckpoint:
    def test_valid_checkpoint(self):
        cp = {
            "id": "cp2",
            "channel_values": {"messages": [1, 2]},
        }
        result = _summarize_checkpoint(cp)
        assert result["checkpoint_id"] == "cp2"
        assert result["n_messages"] == 2

    def test_none_checkpoint(self):
        result = _summarize_checkpoint(None)
        assert result == {}

    def test_non_dict(self):
        result = _summarize_checkpoint("string")
        assert result == {}


# ------------------------------------------------------------------
# attach_tracing 补丁测试
# ------------------------------------------------------------------


def _make_saver(fail: bool = False):
    """构造一个 mock saver，模拟 PostgresSaver 行为。"""
    saver = MagicMock()
    saver._tracing_attached = False

    def _get_tuple(config):
        if fail:
            raise ValueError("get_tuple failed")
        return None  # miss

    def _put(config, checkpoint, metadata, new_versions):
        if fail:
            raise ValueError("put failed")
        return {"checkpoint_id": "cp_new"}

    def _put_writes(config, writes, task_id, task_path=""):
        if fail:
            raise ValueError("put_writes failed")

    saver.get_tuple = _get_tuple
    saver.put = _put
    saver.put_writes = _put_writes
    return saver


def _make_middleware() -> TimingMiddleware:
    mw = TimingMiddleware(agent_name="test_agent")
    mock_store = MagicMock()
    mock_store.push.return_value = None
    mw._store = mock_store  # type: ignore[attr-defined]
    return mw


class TestAttachTracing:
    def test_idempotent(self):
        saver = _make_saver()
        mw = _make_middleware()
        attach_tracing(saver, mw)
        attach_tracing(saver, mw)  # second call should be noop
        # _tracing_attached remains True
        assert saver._tracing_attached is True

    def test_get_tuple_ok(self):
        saver = _make_saver()
        mw = _make_middleware()
        attach_tracing(saver, mw)
        config = {"configurable": {"thread_id": "t1"}}
        result = saver.get_tuple(config)
        assert result is None  # miss as designed

    def test_get_tuple_error_reraises(self):
        saver = _make_saver(fail=True)
        mw = _make_middleware()
        attach_tracing(saver, mw)
        with pytest.raises(ValueError, match="get_tuple failed"):
            saver.get_tuple({"configurable": {}})

    def test_put_ok(self):
        saver = _make_saver()
        mw = _make_middleware()
        attach_tracing(saver, mw)
        config = {"configurable": {"thread_id": "t1"}}
        cp = {"id": "cp1", "channel_values": {"messages": []}}
        result = saver.put(config, cp, {"step": 1, "source": "input"}, {})
        assert result["checkpoint_id"] == "cp_new"

    def test_put_error_reraises(self):
        saver = _make_saver(fail=True)
        mw = _make_middleware()
        attach_tracing(saver, mw)
        with pytest.raises(ValueError, match="put failed"):
            saver.put({}, {}, {}, {})

    def test_put_writes_ok(self):
        saver = _make_saver()
        mw = _make_middleware()
        attach_tracing(saver, mw)
        saver.put_writes({"configurable": {"thread_id": "t1"}}, [1, 2], "task1")

    def test_put_writes_error_reraises(self):
        saver = _make_saver(fail=True)
        mw = _make_middleware()
        attach_tracing(saver, mw)
        with pytest.raises(ValueError, match="put_writes failed"):
            saver.put_writes({}, [], "task1")

    def test_async_methods_exist(self):
        saver = _make_saver()
        mw = _make_middleware()
        attach_tracing(saver, mw)
        assert asyncio.iscoroutinefunction(saver.aget_tuple)
        assert asyncio.iscoroutinefunction(saver.aput)
        assert asyncio.iscoroutinefunction(saver.aput_writes)

    def test_aget_tuple_ok(self):
        saver = _make_saver()
        mw = _make_middleware()
        attach_tracing(saver, mw)
        result = asyncio.get_event_loop().run_until_complete(
            saver.aget_tuple({"configurable": {"thread_id": "t1"}})
        )
        assert result is None

    def test_aput_ok(self):
        saver = _make_saver()
        mw = _make_middleware()
        attach_tracing(saver, mw)
        cp = {"id": "cp2", "channel_values": {}}
        result = asyncio.get_event_loop().run_until_complete(
            saver.aput({"configurable": {}}, cp, {}, {})
        )
        assert result["checkpoint_id"] == "cp_new"

    def test_aput_writes_ok(self):
        saver = _make_saver()
        mw = _make_middleware()
        attach_tracing(saver, mw)
        asyncio.get_event_loop().run_until_complete(
            saver.aput_writes({"configurable": {}}, [], "task_x")
        )

    def test_span_emitted_on_get_tuple(self):
        """attach 后调用 get_tuple 时 _emit 应被调用一次。"""
        saver = _make_saver()
        mw = _make_middleware()
        emitted: list[SpanEvent] = []
        mw._emit = emitted.append  # type: ignore[method-assign]
        attach_tracing(saver, mw)
        saver.get_tuple({"configurable": {"thread_id": "t1"}})
        assert len(emitted) == 1
        assert emitted[0].span_type == "checkpoint"
        assert emitted[0].name == "get_tuple"
        assert emitted[0].status == "ok"

    def test_span_status_error_on_exception(self):
        saver = _make_saver(fail=True)
        mw = _make_middleware()
        emitted: list[SpanEvent] = []
        mw._emit = emitted.append  # type: ignore[method-assign]
        attach_tracing(saver, mw)
        with pytest.raises(ValueError):
            saver.get_tuple({})
        assert len(emitted) == 1
        assert emitted[0].status == "error"
        assert emitted[0].error is not None
