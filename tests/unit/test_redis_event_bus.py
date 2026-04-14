"""RedisEventBus 单元测试（用 fakeredis 替代真实 Redis）。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


@pytest.fixture()
def bus_pair():
    """返回一个 RedisEventBus 实例（sync + async fakeredis 共用同一 server）。"""
    fakeredis = pytest.importorskip("fakeredis")
    from fakeredis import aioredis as fake_aioredis  # noqa: F401

    from core.tasks.events_redis import RedisEventBus

    server = fakeredis.FakeServer()
    bus = RedisEventBus(redis_url="redis://fake", key_prefix="t:ev")
    bus._sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    bus._async = fake_aioredis.FakeRedis(server=server, decode_responses=True)
    return bus


def test_next_seq_monotonic_per_trace(bus_pair) -> None:
    assert bus_pair.next_seq("a") == 1
    assert bus_pair.next_seq("a") == 2
    assert bus_pair.next_seq("b") == 1
    assert bus_pair.next_seq("a") == 3


def test_publish_rings_buffer(bus_pair) -> None:
    bus_pair._buffer_size = 3
    for i in range(5):
        bus_pair.publish("t1", {"type": "s", "seq": i + 1})
    seqs = [e["seq"] for e in bus_pair.buffered("t1")]
    assert seqs == [3, 4, 5]


def test_close_blocks_further_publish(bus_pair) -> None:
    bus_pair.publish("t1", {"type": "s", "seq": 1})
    bus_pair.close("t1")
    bus_pair.publish("t1", {"type": "s", "seq": 2})  # 应被丢弃
    assert [e["seq"] for e in bus_pair.buffered("t1")] == [1]
    assert bus_pair.is_closed("t1")


def test_drop_clears_all(bus_pair) -> None:
    bus_pair.publish("t1", {"type": "s", "seq": 1})
    bus_pair.close("t1")
    bus_pair.drop("t1")
    assert bus_pair.buffered("t1") == []
    assert not bus_pair.is_closed("t1")
    assert bus_pair.next_seq("t1") == 1


@pytest.mark.asyncio
async def test_subscribe_replays_buffer_and_live(bus_pair) -> None:
    bus_pair.publish("t1", {"type": "s", "seq": 1})
    bus_pair.publish("t1", {"type": "s", "seq": 2})

    received: list[dict[str, Any]] = []

    async def consume() -> None:
        async for ev in bus_pair.subscribe("t1"):
            received.append(ev)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)
    bus_pair.publish("t1", {"type": "s", "seq": 3})
    await asyncio.sleep(0.3)
    bus_pair.close("t1")
    await asyncio.wait_for(task, timeout=3.0)

    assert [e["seq"] for e in received] == [1, 2, 3]


@pytest.mark.asyncio
async def test_subscribe_last_event_id_filters(bus_pair) -> None:
    for i in range(5):
        bus_pair.publish("t1", {"type": "s", "seq": i + 1})

    received: list[int] = []

    async def consume() -> None:
        async for ev in bus_pair.subscribe("t1", last_event_id=3):
            received.append(ev["seq"])

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)
    bus_pair.close("t1")
    await asyncio.wait_for(task, timeout=3.0)

    assert received == [4, 5]


@pytest.mark.asyncio
async def test_subscribe_after_close_replays_only(bus_pair) -> None:
    bus_pair.publish("t1", {"type": "s", "seq": 1})
    bus_pair.publish("t1", {"type": "s", "seq": 2})
    bus_pair.close("t1")

    got: list[int] = []
    async for ev in bus_pair.subscribe("t1"):
        got.append(ev["seq"])
    assert got == [1, 2]


def test_redis_errors_are_swallowed_not_raised(bus_pair) -> None:
    """Redis 底层调用抛异常时 publish/close/drop/buffered/is_closed 都应吞掉。"""

    class Broken:
        def __getattr__(self, _name):
            def _raise(*_a, **_kw):
                raise RuntimeError("redis is down")
            return _raise

        def pipeline(self, *_a, **_kw):
            return Broken()

        def execute(self, *_a, **_kw):
            raise RuntimeError("redis is down")

    bus_pair._sync = Broken()
    # 以下调用都不应向上抛异常
    bus_pair.publish("t", {"type": "x", "seq": 1})
    bus_pair.close("t")
    bus_pair.drop("t")
    assert bus_pair.buffered("t") == []
    assert bus_pair.subscriber_count("t") == 0
    assert bus_pair.is_closed("t") is False


def test_ensure_sync_and_async_lazily_create_clients() -> None:
    """初次访问时才连接；两次访问复用实例（惰性）。"""
    from core.tasks.events_redis import RedisEventBus

    bus = RedisEventBus(redis_url="redis://127.0.0.1:6390/0", key_prefix="x")
    assert bus._sync is None and bus._async is None

    s1 = bus._ensure_sync()
    s2 = bus._ensure_sync()
    assert s1 is s2
    # 构造不实际连接；真连通信会在第一个命令才失败，这里不触发
    a1 = bus._ensure_async()
    a2 = bus._ensure_async()
    assert a1 is a2


def test_subscriber_count_reports_zero_when_none(bus_pair) -> None:
    assert bus_pair.subscriber_count("never-subscribed") == 0


@pytest.mark.asyncio
async def test_aclose_releases_clients(bus_pair) -> None:
    bus_pair.publish("t1", {"type": "s", "seq": 1})
    await bus_pair.aclose()
    assert bus_pair._sync is None
    assert bus_pair._async is None


def test_buffered_skips_malformed_entries(bus_pair) -> None:
    """Redis 里如果混入了不合法的 JSON（理论上不会），buffered 应跳过而不是崩。"""
    bus_pair.publish("t1", {"type": "s", "seq": 1})
    # 手动塞一条脏数据
    bus_pair._sync.rpush(bus_pair._buffer_key("t1"), "not-json{broken")
    bus_pair.publish("t1", {"type": "s", "seq": 2})
    out = bus_pair.buffered("t1")
    seqs = [e.get("seq") for e in out]
    assert seqs == [1, 2]


@pytest.mark.asyncio
async def test_subscribe_deduplicates_buffer_and_pubsub(bus_pair) -> None:
    """buffer replay 后又从 pubsub 收到同一条 seq → 应去重不重复 yield。"""
    # 先放一条进 buffer
    bus_pair.publish("t1", {"type": "s", "seq": 1})

    received: list[int] = []

    async def consume() -> None:
        async for ev in bus_pair.subscribe("t1"):
            received.append(ev["seq"])

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)
    # 再发几条新事件
    bus_pair.publish("t1", {"type": "s", "seq": 2})
    bus_pair.publish("t1", {"type": "s", "seq": 3})
    await asyncio.sleep(0.3)
    bus_pair.close("t1")
    await asyncio.wait_for(task, timeout=3.0)

    # seq=1 只来自 buffer replay；seq=2/3 只来自 pubsub
    # 不应有重复
    assert received == [1, 2, 3]
