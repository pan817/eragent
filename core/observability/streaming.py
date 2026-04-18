"""SSE 事件总线桥接：把 span 起止 / 阶段节点同步广播给 EventBus。

异步分析接口（/analyze/async）订阅这些事件走 SSE。observability 主流程
不依赖 EventBus，如果未初始化则所有 publish 为 no-op。
"""

from __future__ import annotations

from typing import Any

from core.time_utils import now_cn


def _publish_to_event_bus(trace_id: str, payload: dict[str, Any]) -> None:
    """向全局 EventBus 发布事件。失败吞掉，不影响 observability 主流程。"""
    try:
        from core.tasks.events import get_event_bus

        bus = get_event_bus()
    except Exception:  # noqa: BLE001
        return
    if bus is None:
        return
    try:
        seq = bus.next_seq(trace_id)
        payload = {
            **payload,
            "trace_id": trace_id,
            "ts": now_cn().isoformat(),
            "seq": seq,
        }
        bus.publish(trace_id, payload)
    except Exception:  # noqa: BLE001
        # 不让事件广播失败影响主流程；降级为 debug 日志
        pass


def _publish_span_start(span_type: str, name: str) -> None:
    """在当前 trace 上发布 span 起始事件（仅 tool / dag.task）。"""
    from core.observability.tracing import _current_trace

    ctx = _current_trace.get()
    if ctx is None:
        return
    from core.observability.display_labels import resolve_tool_label

    if span_type == "tool":
        _publish_to_event_bus(
            ctx.trace_id,
            {
                "type": "tool",
                "action": "start",
                "name": name,
                "label": resolve_tool_label(name),
            },
        )
    elif span_type == "dag.task":
        tool_key = name.split(":", 1)[1] if ":" in name else name
        _publish_to_event_bus(
            ctx.trace_id,
            {
                "type": "dag_task",
                "action": "start",
                "task_name": name,
                "label": resolve_tool_label(tool_key),
            },
        )


def _publish_span_end(
    span_type: str,
    name: str,
    *,
    duration_ms: float,
    status: str,
) -> None:
    """在当前 trace 上发布 span 结束事件（仅 tool / dag.task）。"""
    from core.observability.tracing import _current_trace

    ctx = _current_trace.get()
    if ctx is None:
        return
    from core.observability.display_labels import resolve_tool_label

    if span_type == "tool":
        _publish_to_event_bus(
            ctx.trace_id,
            {
                "type": "tool",
                "action": "end",
                "name": name,
                "label": resolve_tool_label(name),
                "duration_ms": duration_ms,
                "status": status,
            },
        )
    elif span_type == "dag.task":
        tool_key = name.split(":", 1)[1] if ":" in name else name
        _publish_to_event_bus(
            ctx.trace_id,
            {
                "type": "dag_task",
                "action": "end",
                "task_name": name,
                "label": resolve_tool_label(tool_key),
                "duration_ms": duration_ms,
                "status": status,
            },
        )


def publish_stage(name: str, attrs: dict[str, Any] | None = None) -> None:
    """供编排层调用：发布阶段事件（intent_resolved / dag_planned / react_started 等）。

    在当前无活跃 trace 时 no-op。
    """
    from core.observability.tracing import _current_trace

    ctx = _current_trace.get()
    if ctx is None:
        return
    from core.observability.display_labels import resolve_stage_label

    payload: dict[str, Any] = {
        "type": "stage",
        "name": name,
        "label": resolve_stage_label(name),
    }
    if attrs:
        payload["attrs"] = attrs
    _publish_to_event_bus(ctx.trace_id, payload)
