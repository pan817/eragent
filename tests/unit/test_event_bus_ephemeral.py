"""EventBus ephemeral 发布机制单元测试（RedisEventBus + fakeredis）。"""

from __future__ import annotations

import pytest


@pytest.fixture()
def bus():
    fakeredis = pytest.importorskip("fakeredis")
    from fakeredis import aioredis as fake_aioredis

    from core.tasks.events_redis import RedisEventBus

    server = fakeredis.FakeServer()
    b = RedisEventBus(redis_url="redis://fake", key_prefix="t:eph")
    b._sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    b._async = fake_aioredis.FakeRedis(server=server, decode_responses=True)
    return b


def test_ephemeral_skips_buffer(bus) -> None:
    """ephemeral=True 的事件不写入环形缓冲。"""
    trace = "t-eph-1"

    bus.publish(trace, {"type": "status", "trace_id": trace, "ts": "ts", "seq": 1, "replay_safe": True})
    bus.publish(
        trace,
        {"type": "chunk", "trace_id": trace, "ts": "ts", "seq": 0, "replay_safe": False, "delta": "hi"},
        ephemeral=True,
    )
    bus.publish(trace, {"type": "done", "trace_id": trace, "ts": "ts", "seq": 2, "replay_safe": True})

    buffered = bus.buffered(trace)
    types = [ev["type"] for ev in buffered]
    assert types == ["status", "done"]
    assert all(ev["replay_safe"] is True for ev in buffered)


def test_default_publish_writes_to_buffer(bus) -> None:
    """不传 ephemeral（默认 False）时写入缓冲。"""
    trace = "t-eph-3"

    bus.publish(trace, {"type": "stage", "trace_id": trace, "ts": "ts", "seq": 1, "replay_safe": True, "name": "x"})
    buffered = bus.buffered(trace)
    assert len(buffered) == 1
    assert buffered[0]["type"] == "stage"
    assert buffered[0]["replay_safe"] is True


def test_ephemeral_not_in_buffer_for_late_subscriber(bus) -> None:
    """迟到的订阅者不会重放曾经发过的 ephemeral 事件（因为不在 buffer 里）。"""
    trace = "t-eph-4"

    bus.publish(trace, {"type": "status", "trace_id": trace, "ts": "ts", "seq": 1, "replay_safe": True})
    bus.publish(
        trace,
        {"type": "chunk", "trace_id": trace, "ts": "ts", "seq": 0, "replay_safe": False, "delta": "x"},
        ephemeral=True,
    )
    buffered = bus.buffered(trace)
    assert [ev["type"] for ev in buffered] == ["status"]
    assert buffered[0]["replay_safe"] is True
