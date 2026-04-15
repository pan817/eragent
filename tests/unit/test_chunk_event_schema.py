"""ChunkEvent schema 单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.tasks.schemas import ChunkEvent


def test_chunk_event_default_fields() -> None:
    ev = ChunkEvent(
        trace_id="t-1",
        seq=0,
        node="report",
        message_id="10087",
        delta="hello",
        index=0,
    )
    assert ev.type == "chunk"
    assert ev.seq == 0  # 协议约定固定 0
    assert ev.node == "report"
    assert ev.message_id == "10087"
    assert ev.delta == "hello"
    assert ev.index == 0
    assert ev.eos is False  # 默认值


def test_chunk_event_eos_true() -> None:
    ev = ChunkEvent(
        trace_id="t-1",
        seq=0,
        node="report",
        message_id="10087",
        delta="",
        index=5,
        eos=True,
    )
    assert ev.eos is True
    assert ev.delta == ""  # eos 帧通常 delta 为空串，协议允许


def test_chunk_event_missing_required_fields() -> None:
    # 缺 message_id
    with pytest.raises(ValidationError):
        ChunkEvent(  # type: ignore[call-arg]
            trace_id="t-1",
            seq=0,
            node="report",
            delta="x",
            index=0,
        )
    # 缺 node
    with pytest.raises(ValidationError):
        ChunkEvent(  # type: ignore[call-arg]
            trace_id="t-1",
            seq=0,
            message_id="m",
            delta="x",
            index=0,
        )


def test_chunk_event_serializes_with_seq_zero() -> None:
    """ChunkEvent 序列化后 seq=0 字段必须存在（与 heartbeat 对齐）。"""
    ev = ChunkEvent(
        trace_id="t-1",
        seq=0,
        node="report",
        message_id="m",
        delta="x",
        index=0,
    )
    dumped = ev.model_dump(mode="json")
    assert dumped["type"] == "chunk"
    assert dumped["seq"] == 0
    assert dumped["eos"] is False
    assert "ts" in dumped  # BaseEvent 默认注入


def test_chunk_event_node_accepts_report_and_agent_final() -> None:
    """Phase 1 / Phase 2 的 node 取值都应该可接受（字符串字段不做枚举约束）。"""
    ev_report = ChunkEvent(
        trace_id="t", seq=0, node="report", message_id="m", delta="", index=0,
    )
    ev_agent = ChunkEvent(
        trace_id="t", seq=0, node="agent_final", message_id="m", delta="", index=0,
    )
    assert ev_report.node == "report"
    assert ev_agent.node == "agent_final"
