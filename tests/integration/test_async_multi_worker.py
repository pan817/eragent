"""多 worker 集成测试：验证 RedisEventBus 跨进程共享事件。

思路：构造两个独立的 ``RedisEventBus`` 实例（代表两个 worker 进程），
底层共用一个 ``fakeredis.FakeServer``。发布方和订阅方分别落在不同实例上，
模拟前端报告的"POST 落在 worker A、SSE 落在 worker B"场景。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


def _make_bus(server, prefix: str = "mw:ev"):
    """基于给定 FakeServer 创建一个 RedisEventBus（sync + async 双 client）。"""
    fakeredis = pytest.importorskip("fakeredis")
    from fakeredis import aioredis as fake_aioredis

    from core.tasks.events_redis import RedisEventBus

    bus = RedisEventBus(redis_url="redis://fake", key_prefix=prefix)
    bus._sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    bus._async = fake_aioredis.FakeRedis(server=server, decode_responses=True)
    return bus


@pytest.fixture()
def shared_server():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeServer()


@pytest.mark.asyncio
async def test_cross_worker_publish_reaches_subscriber(shared_server) -> None:
    """发布方（worker A）publish → 订阅方（worker B）subscribe 应收到事件。"""
    bus_a = _make_bus(shared_server)  # "worker A" 发布方
    bus_b = _make_bus(shared_server)  # "worker B" 订阅方

    received: list[dict[str, Any]] = []

    async def consume() -> None:
        async for ev in bus_b.subscribe("trace-1"):
            received.append(ev)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)  # 等订阅建立

    # worker A 发布序列
    bus_a.publish("trace-1", {"type": "status", "seq": 1, "state": "running"})
    bus_a.publish("trace-1", {"type": "stage", "seq": 2, "name": "intent_resolved"})
    bus_a.publish("trace-1", {"type": "tool", "seq": 3, "action": "start"})
    bus_a.publish("trace-1", {"type": "done", "seq": 4, "status": "ok"})
    await asyncio.sleep(0.3)

    # worker A close → worker B 的 subscribe 应退出
    bus_a.close("trace-1")
    await asyncio.wait_for(task, timeout=3.0)

    seqs = [e["seq"] for e in received]
    assert seqs == [1, 2, 3, 4]
    types = [e["type"] for e in received]
    assert types == ["status", "stage", "tool", "done"]


@pytest.mark.asyncio
async def test_seq_is_monotonic_across_workers(shared_server) -> None:
    """两个 worker 交替 next_seq 应得到全局单调序列。"""
    bus_a = _make_bus(shared_server)
    bus_b = _make_bus(shared_server)

    assert bus_a.next_seq("t") == 1
    assert bus_b.next_seq("t") == 2
    assert bus_a.next_seq("t") == 3
    assert bus_b.next_seq("t") == 4
    assert bus_a.next_seq("t") == 5


@pytest.mark.asyncio
async def test_late_subscriber_replays_from_buffer(shared_server) -> None:
    """worker B 在 worker A 发布若干事件后才订阅，仍能从 Redis buffer 拿到。"""
    bus_a = _make_bus(shared_server)
    bus_b = _make_bus(shared_server)

    # worker A 先发布几条
    for i in range(3):
        bus_a.publish("trace-late", {"type": "x", "seq": i + 1})

    received: list[int] = []

    async def consume() -> None:
        async for ev in bus_b.subscribe("trace-late"):
            received.append(ev["seq"])

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)
    # worker A 后续再发一条 + close
    bus_a.publish("trace-late", {"type": "x", "seq": 4})
    await asyncio.sleep(0.3)
    bus_a.close("trace-late")
    await asyncio.wait_for(task, timeout=3.0)

    assert received == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_subscriber_after_task_done_still_gets_full_history(
    shared_server,
) -> None:
    """worker A 任务跑完并 close 后，worker B 再订阅应 replay 完整历史然后退出。"""
    bus_a = _make_bus(shared_server)
    bus_b = _make_bus(shared_server)

    bus_a.publish("trace-late", {"type": "status", "seq": 1, "state": "running"})
    bus_a.publish("trace-late", {"type": "done", "seq": 2, "status": "ok"})
    bus_a.close("trace-late")

    got: list[dict[str, Any]] = []
    async for ev in bus_b.subscribe("trace-late"):
        got.append(ev)
    assert [e["seq"] for e in got] == [1, 2]
    assert got[-1]["type"] == "done"
