"""控制台树形 + 汇总输出。"""

from __future__ import annotations

from core.observability.store import SpanEvent


def format_tree(spans: list[SpanEvent], trace_id: str) -> str:
    """构造一棵 trace 树的多行字符串。"""
    by_parent: dict[str | None, list[SpanEvent]] = {}
    for sp in spans:
        by_parent.setdefault(sp.parent_span_id, []).append(sp)
    for k in by_parent:
        by_parent[k].sort(key=lambda s: s.started_at)

    lines: list[str] = [f"=== trace {trace_id} ==="]

    def walk(parent_id: str | None, prefix: str) -> None:
        children = by_parent.get(parent_id, [])
        for i, sp in enumerate(children):
            last = i == len(children) - 1
            branch = "└─ " if last else "├─ "
            status_tag = "" if sp.status == "ok" else f" [{sp.status}]"
            extra = ""
            if sp.span_type == "tool":
                extra = f"  args={_truncate(sp.attributes.get('args'))}"
            elif sp.span_type == "model":
                extra = f"  msgs={sp.attributes.get('message_count', '?')}"
            lines.append(
                f"{prefix}{branch}[{sp.span_type}] {sp.name}  {sp.duration_ms:.1f}ms{status_tag}{extra}"
            )
            walk(sp.span_id, prefix + ("   " if last else "│  "))

    walk(None, "")
    return "\n".join(lines)


def format_summary(spans: list[SpanEvent], total_ms: float) -> str:
    """统计 model / tool 调用次数与累计耗时。"""
    model_count = sum(1 for s in spans if s.span_type == "model")
    tool_count = sum(1 for s in spans if s.span_type == "tool")
    model_ms = sum(s.duration_ms for s in spans if s.span_type == "model")
    tool_ms = sum(s.duration_ms for s in spans if s.span_type == "tool")
    return (
        f"--- summary: total={total_ms:.1f}ms  "
        f"model={model_count}({model_ms:.1f}ms)  "
        f"tool={tool_count}({tool_ms:.1f}ms) ---"
    )


def _truncate(value: object, length: int = 60) -> str:
    s = str(value) if value is not None else ""
    return s if len(s) <= length else s[: length - 3] + "..."
