"""EventBus 工厂函数单元测试。"""

from __future__ import annotations

import pytest


def test_init_event_bus_creates_redis_bus() -> None:
    from core.tasks.events import get_event_bus, init_event_bus, shutdown_event_bus
    from core.tasks.events_redis import RedisEventBus

    shutdown_event_bus()
    bus = init_event_bus(redis_url="redis://fake", redis_key_prefix="x")
    assert isinstance(bus, RedisEventBus)
    assert get_event_bus() is bus
    # idempotent
    assert init_event_bus(redis_url="redis://fake") is bus
    shutdown_event_bus()


def test_init_event_bus_requires_redis_url() -> None:
    from core.tasks.events import init_event_bus, shutdown_event_bus

    shutdown_event_bus()
    with pytest.raises(ValueError, match="redis_url"):
        init_event_bus(redis_url="")


def test_shutdown_clears_singleton() -> None:
    from core.tasks.events import get_event_bus, init_event_bus, shutdown_event_bus

    shutdown_event_bus()
    init_event_bus(redis_url="redis://fake")
    assert get_event_bus() is not None
    shutdown_event_bus()
    assert get_event_bus() is None
