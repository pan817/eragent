"""EventBus ephemeral 发布机制单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from core.tasks.events import MemoryEventBus


@pytest.mark.asyncio
async def test_memory_bus_ephemeral_skips_buffer() -> None:
    """ephemeral=True 的事件不写入环形缓冲。"""
    bus = MemoryEventBus(buffer_size=10)
    trace = "t-eph-1"

    bus.publish(
        trace,
        {"type": "status", "trace_id": trace, "ts": "ts", "seq": 1},
    )
    bus.publish(
        trace,
        {"type": "chunk", "trace_id": trace, "ts": "ts", "seq": 0, "delta": "hi"},
        ephemeral=True,
    )
    bus.publish(
        trace,
        {"type": "done", "trace_id": trace, "ts": "ts", "seq": 2},
    )

    buffered = bus.buffered(trace)
    # chunk 事件不应出现在 buffer 中
    types = [ev["type"] for ev in buffered]
    assert types == ["status", "done"]


@pytest.mark.asyncio
async def test_memory_bus_ephemeral_still_delivered_to_live_subscribers() -> None:
    """ephemeral=True 的事件仍然推送给活跃订阅者。"""
    bus = MemoryEventBus(buffer_size=10)
    trace = "t-eph-2"

    received: list[dict] = []

    async def _consumer() -> None:
        async for ev in bus.subscribe(trace):
            received.append(ev)
            if ev.get("type") == "done":
                break

    task = asyncio.create_task(_consumer())
    # 让订阅建立
    await asyncio.sleep(0.01)

    bus.publish(
        trace,
        {"type": "chunk", "trace_id": trace, "ts": "ts", "seq": 0, "delta": "a"},
        ephemeral=True,
    )
    bus.publish(
        trace,
        {"type": "chunk", "trace_id": trace, "ts": "ts", "seq": 0, "delta": "b"},
        ephemeral=True,
    )
    bus.publish(
        trace,
        {"type": "done", "trace_id": trace, "ts": "ts", "seq": 1},
    )

    await asyncio.wait_for(task, timeout=1.0)

    types = [ev["type"] for ev in received]
    # 所有 chunk + done 都应被订阅者收到
    assert types == ["chunk", "chunk", "done"]


@pytest.mark.asyncio
async def test_memory_bus_default_publish_unchanged() -> None:
    """不传 ephemeral（默认 False）时，行为与改造前一致。"""
    bus = MemoryEventBus(buffer_size=10)
    trace = "t-eph-3"

    bus.publish(
        trace,
        {"type": "stage", "trace_id": trace, "ts": "ts", "seq": 1, "name": "x"},
    )
    buffered = bus.buffered(trace)
    assert len(buffered) == 1
    assert buffered[0]["type"] == "stage"


def test_memory_bus_ephemeral_replay_skipped_on_late_subscribe() -> None:
    """迟到的订阅者不会重放曾经发过的 ephemeral 事件（因为不在 buffer 里）。"""
    bus = MemoryEventBus(buffer_size=10)
    trace = "t-eph-4"

    bus.publish(
        trace,
        {"type": "status", "trace_id": trace, "ts": "ts", "seq": 1},
    )
    # 这条 chunk 不会进 buffer
    bus.publish(
        trace,
        {"type": "chunk", "trace_id": trace, "ts": "ts", "seq": 0, "delta": "x"},
        ephemeral=True,
    )
    buffered = bus.buffered(trace)
    assert [ev["type"] for ev in buffered] == ["status"]
