"""EventBus 单元测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.tasks.events import EventBus


def _make_event(seq: int, type_: str = "status", **extra: Any) -> dict[str, Any]:
    return {"type": type_, "trace_id": "t1", "ts": "2026-04-14T10:00:00Z", "seq": seq, **extra}


@pytest.mark.asyncio
async def test_publish_then_subscribe_replays_from_buffer() -> None:
    bus = EventBus(buffer_size=10)
    for i in range(3):
        bus.publish("t1", _make_event(i + 1))

    received: list[dict[str, Any]] = []

    async def consume() -> None:
        async for ev in bus.subscribe("t1"):
            received.append(ev)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # 让订阅 replay 完
    bus.close("t1")
    await asyncio.wait_for(task, timeout=1.0)

    assert [e["seq"] for e in received] == [1, 2, 3]


@pytest.mark.asyncio
async def test_last_event_id_filters_replay() -> None:
    bus = EventBus(buffer_size=10)
    for i in range(5):
        bus.publish("t1", _make_event(i + 1))

    received: list[dict[str, Any]] = []

    async def consume() -> None:
        async for ev in bus.subscribe("t1", last_event_id=3):
            received.append(ev)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    bus.close("t1")
    await asyncio.wait_for(task, timeout=1.0)

    assert [e["seq"] for e in received] == [4, 5]


@pytest.mark.asyncio
async def test_live_fanout_to_multiple_subscribers() -> None:
    bus = EventBus(buffer_size=10)
    received_a: list[int] = []
    received_b: list[int] = []

    async def consume(dest: list[int]) -> None:
        async for ev in bus.subscribe("t1"):
            dest.append(ev["seq"])

    task_a = asyncio.create_task(consume(received_a))
    task_b = asyncio.create_task(consume(received_b))
    await asyncio.sleep(0.05)

    for i in range(3):
        bus.publish("t1", _make_event(i + 1))
    await asyncio.sleep(0.05)
    bus.close("t1")
    await asyncio.gather(task_a, task_b)

    assert received_a == [1, 2, 3]
    assert received_b == [1, 2, 3]


@pytest.mark.asyncio
async def test_close_ends_all_subscribers() -> None:
    bus = EventBus()
    finished = asyncio.Event()

    async def consume() -> None:
        async for _ in bus.subscribe("t1"):
            pass
        finished.set()

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    assert bus.subscriber_count("t1") == 1
    bus.close("t1")
    await asyncio.wait_for(finished.wait(), timeout=1.0)
    await task


def test_ring_buffer_evicts_oldest() -> None:
    bus = EventBus(buffer_size=3)
    for i in range(5):
        bus.publish("t1", _make_event(i + 1))
    seqs = [e["seq"] for e in bus.buffered("t1")]
    assert seqs == [3, 4, 5]


def test_next_seq_is_monotonic_per_trace() -> None:
    bus = EventBus()
    assert bus.next_seq("a") == 1
    assert bus.next_seq("a") == 2
    assert bus.next_seq("b") == 1
    assert bus.next_seq("a") == 3


def test_drop_clears_state() -> None:
    bus = EventBus()
    bus.publish("t1", _make_event(1))
    bus.close("t1")
    assert bus.is_closed("t1")
    bus.drop("t1")
    assert not bus.is_closed("t1")
    assert bus.buffered("t1") == []


@pytest.mark.asyncio
async def test_slow_subscriber_does_not_block_publisher() -> None:
    """队列满时 publisher 不应阻塞；旧事件被丢弃、新事件可继续送达。"""
    bus = EventBus(buffer_size=50, subscriber_queue_size=2)

    first_received: asyncio.Event = asyncio.Event()

    async def slow_consume() -> None:
        async for _ in bus.subscribe("t1"):
            if not first_received.is_set():
                first_received.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(slow_consume())
    # 连续 publish 5 条；若 publisher 阻塞，以下循环会卡住
    for i in range(5):
        bus.publish("t1", _make_event(i + 1))
    await asyncio.sleep(0.05)
    assert first_received.is_set()
    # 缓冲里全量仍在（用于断线重连重放）
    assert len(bus.buffered("t1")) == 5

    bus.close("t1")
    try:
        await asyncio.wait_for(task, timeout=0.5)
    except asyncio.TimeoutError:
        task.cancel()


@pytest.mark.asyncio
async def test_close_with_full_queue_still_delivers_sentinel() -> None:
    bus = EventBus(subscriber_queue_size=1)
    received_end: asyncio.Event = asyncio.Event()

    async def consume() -> None:
        async for _ in bus.subscribe("t1"):
            # 只消费一条就阻塞，让 queue 再次填满
            await asyncio.sleep(0.5)
        received_end.set()

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    bus.publish("t1", _make_event(1))
    bus.publish("t1", _make_event(2))  # 此时订阅 queue 已满
    bus.close("t1")
    await asyncio.wait_for(received_end.wait(), timeout=2.0)
    await task


@pytest.mark.asyncio
async def test_subscribe_after_close_returns_buffered_and_exits() -> None:
    bus = EventBus()
    bus.publish("t1", _make_event(1))
    bus.publish("t1", _make_event(2))
    bus.close("t1")

    collected: list[int] = []
    async for ev in bus.subscribe("t1"):
        collected.append(ev["seq"])
    assert collected == [1, 2]
