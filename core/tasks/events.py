"""EventBus：按 trace_id 维度的 pub/sub 总线。

提供两种实现：
- :class:`MemoryEventBus`：进程内 asyncio.Queue，仅适用于单 worker 部署
- :class:`RedisEventBus`（``events_redis``）：Redis Pub/Sub + 环形缓冲，
  跨 worker / 跨机共享，多 worker 部署必须使用

所有实现都遵循 :class:`EventBusProtocol`，TaskRegistry / SSE 端点
只依赖 Protocol，不关心具体后端。
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from core.logging_utils import get_logger

_logger = get_logger(__name__)


DEFAULT_BUFFER_SIZE = 200
DEFAULT_SUBSCRIBER_QUEUE_SIZE = 200


@runtime_checkable
class EventBusProtocol(Protocol):
    """EventBus 的结构化契约。"""

    def next_seq(self, trace_id: str) -> int: ...

    def publish(self, trace_id: str, event: dict[str, Any]) -> None: ...

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


class MemoryEventBus:
    """按 trace_id 维度的进程内事件总线（asyncio.Queue 实现）。

    所有方法线程安全；订阅 / 事件转发基于 asyncio，必须在事件循环内调用。
    仅适用于单 worker 部署——多 worker 场景请使用 ``RedisEventBus``。
    """

    def __init__(
        self,
        *,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        subscriber_queue_size: int = DEFAULT_SUBSCRIBER_QUEUE_SIZE,
    ) -> None:
        self._buffer_size = buffer_size
        self._subscriber_queue_size = subscriber_queue_size
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any] | None]]] = {}
        self._buffers: dict[str, deque[dict[str, Any]]] = {}
        self._seq_counters: dict[str, int] = {}
        self._closed: set[str] = set()

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    def next_seq(self, trace_id: str) -> int:
        """分配一个 trace 维度单调递增的 seq。"""
        with self._lock:
            seq = self._seq_counters.get(trace_id, 0) + 1
            self._seq_counters[trace_id] = seq
            return seq

    def publish(self, trace_id: str, event: dict[str, Any]) -> None:
        """发布事件到指定 trace_id 的所有订阅者 + 环形缓冲。

        ``event`` 应当已经包含 ``type/trace_id/ts/seq`` 字段。
        """
        with self._lock:
            if trace_id in self._closed:
                return
            buf = self._buffers.setdefault(
                trace_id, deque(maxlen=self._buffer_size)
            )
            buf.append(event)
            subs = list(self._subscribers.get(trace_id, ()))
        dropped = 0
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                    dropped += 1
                except asyncio.QueueFull:
                    dropped += 1
        if dropped:
            _logger.warning(
                "event bus dropped %d event(s) for slow subscribers "
                "on trace %s",
                dropped,
                trace_id,
            )

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        trace_id: str,
        *,
        last_event_id: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """订阅指定 trace_id 的事件流。

        - 如果指定了 ``last_event_id``（> 0），会先从环形缓冲重放 seq > last_event_id 的事件
        - 之后进入长轮询模式，从订阅 Queue 拉取
        - ``None`` 作为结束信号：内部取出后生成器自然结束

        调用方应当使用 ``async for event in bus.subscribe(trace_id):`` 消费。
        """
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=self._subscriber_queue_size
        )
        replay: list[dict[str, Any]] = []
        with self._lock:
            if trace_id in self._closed:
                # 已结束：仅重放缓冲即可退出
                buf = self._buffers.get(trace_id, ())
                replay = [e for e in buf if _seq_gt(e, last_event_id)]
                yielded_from_buffer_only = True
            else:
                yielded_from_buffer_only = False
                buf = self._buffers.get(trace_id, ())
                replay = [e for e in buf if _seq_gt(e, last_event_id)]
                self._subscribers.setdefault(trace_id, []).append(q)

        try:
            for ev in replay:
                yield ev
            if yielded_from_buffer_only:
                return
            while True:
                item = await q.get()
                if item is None:
                    return
                yield item
        finally:
            with self._lock:
                subs = self._subscribers.get(trace_id)
                if subs is not None:
                    try:
                        subs.remove(q)
                    except ValueError:
                        pass
                    if not subs:
                        self._subscribers.pop(trace_id, None)

    # ------------------------------------------------------------------
    # 结束
    # ------------------------------------------------------------------

    def close(self, trace_id: str) -> None:
        """标记 trace 已结束：唤醒所有当前订阅者并让新订阅立即退出。"""
        with self._lock:
            self._closed.add(trace_id)
            subs = list(self._subscribers.get(trace_id, ()))
        for q in subs:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                # 替换掉一个旧事件以放入结束信号
                try:
                    _ = q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    _logger.warning(
                        "failed to deliver terminal sentinel to subscriber on trace %s",
                        trace_id,
                    )

    def drop(self, trace_id: str) -> None:
        """完全移除 trace 的缓冲、订阅列表和 seq 计数（TTL 到期后调用）。"""
        with self._lock:
            self._buffers.pop(trace_id, None)
            self._seq_counters.pop(trace_id, None)
            self._closed.discard(trace_id)
            # 残留订阅者（理论上应已断开）发结束信号后移除
            subs = self._subscribers.pop(trace_id, [])
        for q in subs:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------
    # 内省（测试 / 监控用）
    # ------------------------------------------------------------------

    def buffered(self, trace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._buffers.get(trace_id, ()))

    def subscriber_count(self, trace_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(trace_id, ()))

    def is_closed(self, trace_id: str) -> bool:
        with self._lock:
            return trace_id in self._closed


def _seq_gt(event: dict[str, Any], last_event_id: int | None) -> bool:
    if last_event_id is None or last_event_id <= 0:
        return True
    seq = event.get("seq")
    if not isinstance(seq, int):
        return True
    return seq > last_event_id


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------


_bus: EventBusProtocol | None = None


def init_event_bus(
    *,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    subscriber_queue_size: int = DEFAULT_SUBSCRIBER_QUEUE_SIZE,
    backend: str = "memory",
    redis_url: str | None = None,
    redis_key_prefix: str = "eragent:events",
) -> EventBusProtocol:
    """初始化全局 EventBus 单例（幂等）。

    Args:
        buffer_size: 每 trace_id 环形缓冲保留的事件条数（用于断线重连重放）
        subscriber_queue_size: memory 后端下每订阅的 queue 容量
        backend: ``"memory"`` 或 ``"redis"``
        redis_url: Redis 连接串，仅 ``backend="redis"`` 时必须
        redis_key_prefix: Redis key 前缀，多实例隔离用
    """
    global _bus
    if _bus is not None:
        return _bus

    if backend == "redis":
        if not redis_url:
            raise ValueError(
                "event_backend=redis 时必须提供 redis_url "
                "（config.yaml: async_analysis.redis_url）"
            )
        from core.tasks.events_redis import RedisEventBus

        _bus = RedisEventBus(
            redis_url=redis_url,
            key_prefix=redis_key_prefix,
            buffer_size=buffer_size,
            subscriber_queue_size=subscriber_queue_size,
        )
    else:
        _bus = MemoryEventBus(
            buffer_size=buffer_size,
            subscriber_queue_size=subscriber_queue_size,
        )
    return _bus


def get_event_bus() -> EventBusProtocol | None:
    return _bus


# 向后兼容别名：历史代码中的 ``EventBus`` 指向内存实现。
# 新代码应改用 ``EventBusProtocol`` 做类型标注，或直接写 ``MemoryEventBus``。
EventBus = MemoryEventBus


def shutdown_event_bus() -> None:
    global _bus
    _bus = None
