"""RedisEventBus：基于 Redis Pub/Sub + 环形缓冲的跨进程事件总线。

用于 ``uvicorn --workers>1`` 多 worker 部署场景：POST / SSE 请求可能落在
不同 worker 进程，MemoryEventBus 的进程内队列无法跨进程共享；本实现通过
Redis 让所有 worker 看到同一份事件流。

Redis key 布局（按 trace_id 维度）：
- ``{prefix}:channel:{trace_id}`` —— Pub/Sub channel，负责实时事件广播
- ``{prefix}:buffer:{trace_id}`` —— List（RPUSH + LTRIM），环形缓冲用于 Last-Event-ID 重放
- ``{prefix}:seq:{trace_id}``    —— INCR 计数器，跨 worker 单调递增的 seq
- ``{prefix}:closed:{trace_id}`` —— 标记位，set=1 表示该 trace 终态已发布

Pub/Sub 控制消息：
- 普通事件：event JSON（带 seq 字段）
- 结束信号：JSON ``{"__control__": "close", "trace_id": "..."}`` —— 订阅者见到即退出
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from core.logging_utils import get_logger

_logger = get_logger(__name__)


# 终态标记 key 的过期时间：与 TaskRegistry.result_cache_ttl_sec 同数量级即可
# 到期后 Redis 自动清理，避免历史 trace 占用内存
_CLOSED_TTL_SEC = 3600

# 每 trace 的缓冲上限（与 MemoryEventBus 对齐）
DEFAULT_BUFFER_SIZE = 200
DEFAULT_SUBSCRIBER_QUEUE_SIZE = 200

# 结束信号的控制消息载荷
_CONTROL_CLOSE = "__control__close__"


def _seq_gt(event: dict[str, Any], last_event_id: int) -> bool:
    if last_event_id <= 0:
        return True
    seq = event.get("seq")
    if not isinstance(seq, int):
        return True
    return seq > last_event_id


class RedisEventBus:
    """Redis Pub/Sub 实现的跨进程 EventBus。"""

    def __init__(
        self,
        *,
        redis_url: str,
        key_prefix: str = "eragent:events",
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        subscriber_queue_size: int = DEFAULT_SUBSCRIBER_QUEUE_SIZE,
    ) -> None:
        self._redis_url = redis_url
        self._prefix = key_prefix.rstrip(":")
        self._buffer_size = buffer_size
        self._subscriber_queue_size = subscriber_queue_size
        # 同时持有 sync / async 两个 client：
        # - 同步方法（next_seq / publish / close / drop）走 sync client，
        #   让 EventBusProtocol 签名与 MemoryEventBus 对齐，所有调用点零改造；
        # - 异步方法 subscribe 走 async client 用它的 pubsub。
        # 测试可直接赋值 _sync / _async 以注入 fakeredis。
        self._sync: Any = None
        self._async: Any = None

    # ------------------------------------------------------------------
    # Redis client / key 命名辅助
    # ------------------------------------------------------------------

    def _ensure_sync(self) -> Any:
        if self._sync is None:
            from redis import Redis

            self._sync = Redis.from_url(
                self._redis_url, decode_responses=True
            )
        return self._sync

    def _ensure_async(self) -> Any:
        if self._async is None:
            from redis.asyncio import Redis as AsyncRedis

            self._async = AsyncRedis.from_url(
                self._redis_url, decode_responses=True
            )
        return self._async

    def _channel_key(self, trace_id: str) -> str:
        return f"{self._prefix}:channel:{trace_id}"

    def _buffer_key(self, trace_id: str) -> str:
        return f"{self._prefix}:buffer:{trace_id}"

    def _seq_key(self, trace_id: str) -> str:
        return f"{self._prefix}:seq:{trace_id}"

    def _closed_key(self, trace_id: str) -> str:
        return f"{self._prefix}:closed:{trace_id}"

    # ------------------------------------------------------------------
    # 业务方法占位——后续 T4.2 ~ T4.6 逐步填充
    # ------------------------------------------------------------------

    def next_seq(self, trace_id: str) -> int:
        """跨进程单调递增地为 trace 分配下一个 seq。

        Redis 不可达 / AUTH 失败时返回 0（降级），与 ``publish`` 的吞异常策略对齐——
        宁可让事件的 seq=0（前端认作"未知序号"不更新 Last-Event-ID 锚点），
        也不要把一次 Redis 抖动打成 API 500。
        """
        try:
            client = self._ensure_sync()
            return int(client.incr(self._seq_key(trace_id)))
        except Exception:  # noqa: BLE001
            _logger.warning(
                "redis event bus next_seq failed on trace %s (degraded to 0)",
                trace_id,
                exc_info=True,
            )
            return 0

    def publish(
        self,
        trace_id: str,
        event: dict[str, Any],
        *,
        ephemeral: bool = False,
    ) -> None:
        """发布事件到所有订阅者 + 写入环形缓冲。

        - 关闭后的 trace 不再发布（与 MemoryEventBus 语义对齐）
        - 用 pipeline 打包 RPUSH / LTRIM / PUBLISH，减少 RTT
        - 失败吞掉 + WARNING，不影响业务主流程

        Args:
            ephemeral: 若为 True，事件仅 ``PUBLISH`` 给 live 订阅者，**不写入
                环形 LIST**（跳过 RPUSH / LTRIM）。用于 LLM 流式 chunk 等高频、
                丢失可接受的事件，避免短时间内撑爆 Redis 内存。默认 False。
        """
        client = self._ensure_sync()
        try:
            if client.exists(self._closed_key(trace_id)):
                return
            payload = json.dumps(event, ensure_ascii=False, default=str)
            chan_key = self._channel_key(trace_id)
            if ephemeral:
                # 仅广播，不持久化到环形缓冲
                client.publish(chan_key, payload)
                return
            buf_key = self._buffer_key(trace_id)
            pipe = client.pipeline(transaction=False)
            pipe.rpush(buf_key, payload)
            # 保留最新 N 条，LTRIM 的 start/stop 都是闭区间
            pipe.ltrim(buf_key, -self._buffer_size, -1)
            pipe.publish(chan_key, payload)
            pipe.execute()
        except Exception:  # noqa: BLE001
            _logger.warning(
                "redis event bus publish failed on trace %s",
                trace_id,
                exc_info=True,
            )

    async def subscribe(
        self,
        trace_id: str,
        *,
        last_event_id: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """订阅指定 trace 的事件流（async generator）。

        时序策略：
        1. 先订阅 Pub/Sub channel（建立订阅连接，开始缓存新消息）
        2. 再读 buffer 里已有事件（RPUSH 早于本次订阅的事件）
        3. yield buffer 中 seq > last_event_id 的事件
        4. 然后从 pubsub 消费实时事件，用 seq 去重 buffer 已 yield 过的
        5. 收到 ``__control__close__`` 控制消息 → 退出
        6. finally 里 unsubscribe + 关闭 pubsub，避免连接泄漏

        若订阅时 trace 已关闭（closed 标记存在）：只 replay buffer 即退出，
        不开 pubsub 连接。
        """
        last_sent = last_event_id or 0

        # 关闭状态快照：若已关闭则只重放 buffer
        if self.is_closed(trace_id):
            for ev in self.buffered(trace_id):
                if _seq_gt(ev, last_sent):
                    yield ev
            return

        async_client = self._ensure_async()
        pubsub = async_client.pubsub()
        try:
            await pubsub.subscribe(self._channel_key(trace_id))

            # 读 buffer 时订阅已建立，pubsub 会缓存中间发布的消息
            buf = self.buffered(trace_id)
            max_replayed = last_sent
            for ev in buf:
                if _seq_gt(ev, last_sent):
                    yield ev
                    seq = ev.get("seq")
                    if isinstance(seq, int) and seq > max_replayed:
                        max_replayed = seq

            # 进入实时循环。超时只是为了让 async generator 可被及时 cancel
            while True:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if msg is None:
                    continue
                data = msg.get("data")
                if not isinstance(data, str):
                    continue
                if data == _CONTROL_CLOSE:
                    return
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                # 去重：seq ≤ buffer 已 yield 的最大值，跳过。
                # **例外**：seq == 0 的事件（chunk / heartbeat 等 ephemeral
                # 事件）不参与 Last-Event-ID 重放，也不入环形缓冲，因此
                # 天然不会与 buffer 有重复。这些事件必须直接透传，否则
                # 一旦 max_replayed > 0 就会把所有 seq=0 事件吃掉，导致
                # 前端永远收不到 LLM chunk。
                seq = ev.get("seq")
                if (
                    isinstance(seq, int)
                    and seq > 0
                    and seq <= max_replayed
                ):
                    continue
                yield ev
                if isinstance(seq, int) and seq > max_replayed:
                    max_replayed = seq
        finally:
            try:
                await pubsub.unsubscribe(self._channel_key(trace_id))
            except Exception:  # noqa: BLE001
                pass
            try:
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass

    def close(self, trace_id: str) -> None:
        """标记 trace 终态并唤醒所有订阅者退出。

        - ``SET closed:{tid}`` 带 TTL，用于 publish / 新订阅的"已关闭"判断
        - 向 channel 发送控制消息 ``__control__close__``，让当前订阅者感知并退出
        """
        client = self._ensure_sync()
        try:
            pipe = client.pipeline(transaction=False)
            pipe.set(self._closed_key(trace_id), "1", ex=_CLOSED_TTL_SEC)
            pipe.publish(
                self._channel_key(trace_id),
                _CONTROL_CLOSE,
            )
            pipe.execute()
        except Exception:  # noqa: BLE001
            _logger.warning(
                "redis event bus close failed on trace %s",
                trace_id,
                exc_info=True,
            )

    def drop(self, trace_id: str) -> None:
        """删除 trace 的全部 Redis key。TTL 清理或 sweep 周期调用。

        多 worker 下多个 sweeper 都会调用 —— DEL 幂等，无害。
        """
        client = self._ensure_sync()
        try:
            client.delete(
                self._channel_key(trace_id),
                self._buffer_key(trace_id),
                self._seq_key(trace_id),
                self._closed_key(trace_id),
            )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "redis event bus drop failed on trace %s",
                trace_id,
                exc_info=True,
            )

    def buffered(self, trace_id: str) -> list[dict[str, Any]]:
        """返回当前环形缓冲中的所有事件（从旧到新）。"""
        client = self._ensure_sync()
        try:
            raw = client.lrange(self._buffer_key(trace_id), 0, -1) or []
        except Exception:  # noqa: BLE001
            _logger.warning(
                "redis event bus buffered read failed on trace %s",
                trace_id,
                exc_info=True,
            )
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            try:
                out.append(json.loads(item))
            except json.JSONDecodeError:
                # 非法条目忽略（理论上不会有，防御式）
                continue
        return out

    def subscriber_count(self, trace_id: str) -> int:
        """当前 channel 的订阅者数量（由 Redis PUBSUB NUMSUB 返回）。"""
        client = self._ensure_sync()
        try:
            result = client.pubsub_numsub(self._channel_key(trace_id))
            # 返回格式: [(channel_name, count), ...]
            if result:
                return int(result[0][1])
        except Exception:  # noqa: BLE001
            _logger.info(
                "redis pubsub_numsub failed on trace %s",
                trace_id,
                exc_info=True,
            )
        return 0

    def is_closed(self, trace_id: str) -> bool:
        client = self._ensure_sync()
        try:
            return bool(client.exists(self._closed_key(trace_id)))
        except Exception:  # noqa: BLE001
            _logger.info(
                "redis is_closed failed on trace %s", trace_id, exc_info=True
            )
            return False

    def ping(self) -> None:
        """连通性探测。失败直接抛异常（供 lifespan 启动时 fail-fast 调用）。

        与其他方法的"吞异常 + 降级"策略不同：此方法**故意**让异常向上传播，
        因为它只在启动期被调用，配错就应该快速失败、阻止服务起来接流量。
        """
        client = self._ensure_sync()
        client.ping()

    async def aclose(self) -> None:
        """关闭底层 Redis 连接（可选，lifespan shutdown 时调用）。"""
        if self._async is not None:
            try:
                await self._async.aclose()
            except Exception:  # noqa: BLE001
                _logger.info("redis async client aclose failed", exc_info=True)
            self._async = None
        if self._sync is not None:
            try:
                self._sync.close()
            except Exception:  # noqa: BLE001
                _logger.info("redis sync client close failed", exc_info=True)
            self._sync = None
