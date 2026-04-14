"""EventBus：进程内按 trace_id 维度的 pub/sub 总线。

- 用于 SSE 端点消费异步分析任务的进度事件。
- 每个订阅对应一个 ``asyncio.Queue``，发布方 ``put_nowait``，队列满则丢最旧。
- 每 trace_id 维护一个环形缓冲（最近 N 条），支持 ``Last-Event-ID`` 重放。
- 单进程实现，不跨机器广播——当前只需单实例部署。
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from core.logging_utils import get_logger

_logger = get_logger(__name__)


DEFAULT_BUFFER_SIZE = 200
DEFAULT_SUBSCRIBER_QUEUE_SIZE = 200


class EventBus:
    """按 trace_id 维度的进程内事件总线。

    所有方法线程安全；订阅 / 事件转发基于 asyncio，必须在事件循环内调用。
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


_bus: EventBus | None = None


def init_event_bus(
    *,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    subscriber_queue_size: int = DEFAULT_SUBSCRIBER_QUEUE_SIZE,
) -> EventBus:
    """初始化全局 EventBus（幂等）。"""
    global _bus
    if _bus is None:
        _bus = EventBus(
            buffer_size=buffer_size,
            subscriber_queue_size=subscriber_queue_size,
        )
    return _bus


def get_event_bus() -> EventBus | None:
    return _bus


def shutdown_event_bus() -> None:
    global _bus
    _bus = None
