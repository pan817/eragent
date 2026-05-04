"""EventBus：按 trace_id 维度的 pub/sub 总线。

基于 Redis Pub/Sub + 环形缓冲（``events_redis.RedisEventBus``），
支持单 worker 和多 worker 部署。

所有消费方（TaskRegistry / SSE 端点）只依赖 :class:`EventBusProtocol`，
不直接耦合 ``RedisEventBus`` 实现。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from core.logging_utils import get_logger

_logger = get_logger(__name__)


DEFAULT_BUFFER_SIZE = 200


@runtime_checkable
class EventBusProtocol(Protocol):
    """EventBus 的结构化契约。

    事件 payload 协议：所有事件 dict 必须包含 ``replay_safe: bool`` 字段，
    标识该事件是否可在断线重连后安全重放。持久化事件（status / done / stage /
    tool / model / dag_task）为 True；临时高频事件（chunk）为 False。
    消费方据此判断 Last-Event-ID 重放缺失事件时是否需要告警。
    """

    def next_seq(self, trace_id: str) -> int: ...

    def publish(
        self,
        trace_id: str,
        event: dict[str, Any],
        *,
        ephemeral: bool = False,
    ) -> None: ...

    def subscribe(
        self,
        trace_id: str,
        *,
        last_event_id: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    def close(self, trace_id: str) -> None: ...

    def drop(self, trace_id: str) -> None: ...

    def buffered(self, trace_id: str) -> list[dict[str, Any]]: ...

    def subscriber_count(self, trace_id: str) -> int: ...

    def is_closed(self, trace_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------


_bus: EventBusProtocol | None = None


def init_event_bus(
    *,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    redis_url: str,
    redis_key_prefix: str = "eragent:events",
) -> EventBusProtocol:
    """初始化全局 EventBus 单例（幂等）。

    Args:
        buffer_size: 每 trace_id 环形缓冲保留的事件条数（用于断线重连重放）
        redis_url: Redis 连接串（必填）
        redis_key_prefix: Redis key 前缀，多实例隔离用
    """
    global _bus
    if _bus is not None:
        return _bus

    if not redis_url:
        raise ValueError(
            "redis_url is required "
            "(config.yaml: async_analysis.redis_url)"
        )
    from core.tasks.events_redis import RedisEventBus

    _bus = RedisEventBus(
        redis_url=redis_url,
        key_prefix=redis_key_prefix,
        buffer_size=buffer_size,
    )
    return _bus


def get_event_bus() -> EventBusProtocol | None:
    return _bus


def shutdown_event_bus() -> None:
    global _bus
    _bus = None
