"""为 LangGraph checkpointer 注入链路监控。

LangGraph 在 agent 每个节点前后会调用 checkpointer 的 get_tuple / put /
put_writes(及异步版本)读写 Postgres,这部分 I/O 原本完全游离在 TimingMiddleware
之外。本模块通过**实例级方法包装**的方式给已构造的 PostgresSaver 实例打补丁,
把这 6 个方法的耗时、入参关键元信息与输出体量落到当前 trace 下的
``span_type="checkpoint"`` 跨度里。

采用实例包装(而非子类)的原因:
- ``PostgresSaver.from_conn_string(...)`` 返回的已经是具体实例,再包一层子类
  需要复制构造逻辑,风险更高;
- LangGraph 内部是鸭子类型调用,对 ``isinstance`` 依赖弱;
- 实例包装对 agent 代码零侵入,只在构建 checkpointer 时调一次
  ``attach_tracing(saver, middleware)`` 即可。
"""

from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from datetime import datetime
from typing import Any

from core.logging_utils import get_logger
from core.observability.middleware import TimingMiddleware, _current_trace
from core.observability.store import SpanEvent

_logger = get_logger(__name__)


def _truncate(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _thread_id(config: Any) -> str | None:
    try:
        return (config or {}).get("configurable", {}).get("thread_id")  # type: ignore[union-attr]
    except Exception as exc:
        _logger.debug("_thread_id extraction failed: %s", exc)
        return None


def _checkpoint_id(config: Any) -> str | None:
    try:
        return (config or {}).get("configurable", {}).get("checkpoint_id")  # type: ignore[union-attr]
    except Exception as exc:
        _logger.debug("_checkpoint_id extraction failed: %s", exc)
        return None


def _summarize_tuple(result: Any) -> dict[str, Any]:
    """从 CheckpointTuple 提取元信息(不落原文,避免 attributes 爆炸)。"""
    if result is None:
        return {"hit": False}
    info: dict[str, Any] = {"hit": True}
    try:
        cp = getattr(result, "checkpoint", None)
        if isinstance(cp, dict):
            channel_values = cp.get("channel_values") or {}
            messages = channel_values.get("messages")
            if isinstance(messages, list):
                info["n_messages"] = len(messages)
            info["checkpoint_id"] = cp.get("id")
            info["checkpoint_ts"] = cp.get("ts")
        md = getattr(result, "metadata", None)
        if isinstance(md, dict):
            if "step" in md:
                info["step"] = md.get("step")
            if "source" in md:
                info["source"] = md.get("source")
    except Exception as exc:  # noqa: BLE001
        _logger.debug("_summarize_tuple failed: %s", exc)
    return info


def _summarize_checkpoint(checkpoint: Any) -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        if isinstance(checkpoint, dict):
            info["checkpoint_id"] = checkpoint.get("id")
            channel_values = checkpoint.get("channel_values") or {}
            messages = channel_values.get("messages")
            if isinstance(messages, list):
                info["n_messages"] = len(messages)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("_summarize_checkpoint failed: %s", exc)
    return info


def _emit_span(
    middleware: TimingMiddleware,
    *,
    name: str,
    attributes: dict[str, Any],
    started_at: datetime,
    t0: float,
    status: str,
    error: str | None,
) -> None:
    """写入一条 checkpoint span。

    复刻 ``TimingMiddleware._record_span`` 的逻辑,但固定 span_type 为
    ``"checkpoint"``,且不增加 model/tool 计数。contextvar 丢失时退化为
    orphan trace_id(与现有 model/tool 行为一致)。
    """
    ctx = _current_trace.get()
    duration_ms = (time.monotonic() - t0) * 1000
    sp = SpanEvent(
        trace_id=ctx.trace_id if ctx is not None else "orphan",
        span_id=str(uuid.uuid4()),
        parent_span_id=None,
        span_type="checkpoint",
        name=name,
        status=status,
        started_at=started_at,
        finished_at=datetime.utcnow(),
        duration_ms=round(duration_ms, 3),
        attributes=attributes,
        error=error,
    )
    if ctx is not None:
        ctx.spans.append(sp)
    # 直接入 store 队列;若 middleware 未配置 store 会静默丢弃
    middleware._emit(sp)  # noqa: SLF001


def attach_tracing(saver: Any, middleware: TimingMiddleware) -> Any:
    """给 PostgresSaver 实例打上 checkpoint span 补丁(幂等)。

    包装的方法:
    - 同步: ``get_tuple`` / ``put`` / ``put_writes``
    - 异步: ``aget_tuple`` / ``aput`` / ``aput_writes``

    异常会被 **重新抛出**,但先落一条 ``status="error"`` 的 span。
    """
    if getattr(saver, "_tracing_attached", False):
        return saver

    orig_get_tuple = saver.get_tuple
    orig_put = saver.put
    orig_put_writes = saver.put_writes

    # ---- 同步 ----

    def get_tuple(config):  # type: ignore[no-untyped-def]
        started_at = datetime.utcnow()
        t0 = time.monotonic()
        status = "ok"
        error: str | None = None
        attrs: dict[str, Any] = {
            "thread_id": _thread_id(config),
            "checkpoint_id_in": _checkpoint_id(config),
        }
        try:
            result = orig_get_tuple(config)
            attrs.update(_summarize_tuple(result))
            return result
        except BaseException as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}\n{_truncate(traceback.format_exc(limit=3))}"
            raise
        finally:
            _emit_span(
                middleware,
                name="get_tuple",
                attributes=attrs,
                started_at=started_at,
                t0=t0,
                status=status,
                error=error,
            )

    def put(config, checkpoint, metadata, new_versions):  # type: ignore[no-untyped-def]
        started_at = datetime.utcnow()
        t0 = time.monotonic()
        status = "ok"
        error: str | None = None
        attrs: dict[str, Any] = {
            "thread_id": _thread_id(config),
            **_summarize_checkpoint(checkpoint),
        }
        if isinstance(metadata, dict):
            if "step" in metadata:
                attrs["step"] = metadata.get("step")
            if "source" in metadata:
                attrs["source"] = metadata.get("source")
        try:
            return orig_put(config, checkpoint, metadata, new_versions)
        except BaseException as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}\n{_truncate(traceback.format_exc(limit=3))}"
            raise
        finally:
            _emit_span(
                middleware,
                name="put",
                attributes=attrs,
                started_at=started_at,
                t0=t0,
                status=status,
                error=error,
            )

    def put_writes(config, writes, task_id, task_path=""):  # type: ignore[no-untyped-def]
        started_at = datetime.utcnow()
        t0 = time.monotonic()
        status = "ok"
        error: str | None = None
        try:
            n_writes = len(writes) if hasattr(writes, "__len__") else None
        except Exception:  # noqa: BLE001
            n_writes = None
        attrs: dict[str, Any] = {
            "thread_id": _thread_id(config),
            "task_id": task_id,
            "n_writes": n_writes,
        }
        try:
            return orig_put_writes(config, writes, task_id, task_path)
        except BaseException as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}\n{_truncate(traceback.format_exc(limit=3))}"
            raise
        finally:
            _emit_span(
                middleware,
                name="put_writes",
                attributes=attrs,
                started_at=started_at,
                t0=t0,
                status=status,
                error=error,
            )

    # ---- 异步 ----
    # sync PostgresSaver 原生的 aget_tuple/aput/aput_writes 继承自 base,
    # 会直接抛 NotImplementedError。这里改为把同步方法扔进 asyncio.to_thread,
    # 让 AsyncPregelLoop 能正常 await。contextvar 在 to_thread 中会被 Python
    # 3.11+ 自动拷贝,span 归属无需额外处理。
    # 同时 async 包装直接复用已包装的同步方法(已带 tracing),不再单独发 span,
    # 以避免同一次 I/O 产生两条 span。

    async def aget_tuple(config):  # type: ignore[no-untyped-def]
        return await asyncio.to_thread(get_tuple, config)

    async def aput(config, checkpoint, metadata, new_versions):  # type: ignore[no-untyped-def]
        return await asyncio.to_thread(put, config, checkpoint, metadata, new_versions)

    async def aput_writes(config, writes, task_id, task_path=""):  # type: ignore[no-untyped-def]
        return await asyncio.to_thread(
            put_writes, config, writes, task_id, task_path
        )

    # 挂回实例
    saver.get_tuple = get_tuple  # type: ignore[method-assign]
    saver.put = put  # type: ignore[method-assign]
    saver.put_writes = put_writes  # type: ignore[method-assign]
    saver.aget_tuple = aget_tuple  # type: ignore[method-assign]
    saver.aput = aput  # type: ignore[method-assign]
    saver.aput_writes = aput_writes  # type: ignore[method-assign]
    saver._tracing_attached = True  # type: ignore[attr-defined]
    return saver
