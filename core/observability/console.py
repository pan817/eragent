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
    """统计 model / tool / memory 调用次数与累计耗时。"""
    model_count = sum(1 for s in spans if s.span_type == "model")
    tool_count = sum(1 for s in spans if s.span_type == "tool")
    memory_count = sum(1 for s in spans if s.span_type == "memory")
    model_ms = sum(s.duration_ms for s in spans if s.span_type == "model")
    tool_ms = sum(s.duration_ms for s in spans if s.span_type == "tool")
    memory_ms = sum(s.duration_ms for s in spans if s.span_type == "memory")
    return (
        f"--- summary: total={total_ms:.1f}ms  "
        f"model={model_count}({model_ms:.1f}ms)  "
        f"tool={tool_count}({tool_ms:.1f}ms)  "
        f"memory={memory_count}({memory_ms:.1f}ms) ---"
    )


def _truncate(value: object, length: int = 60) -> str:
    s = str(value) if value is not None else ""
    return s if len(s) <= length else s[: length - 3] + "..."


def format_io_panel(span: SpanEvent, max_line: int = 200) -> str:
    """对单次 model / tool 调用打印结构化 I/O 面板。

    在调用结束时立即输出，便于实时观察 prompt / 工具参数 / 返回值。
    输入和输出已在 middleware 阶段截断到 _MAX_IO_TEXT，此处再做行宽截断。
    """
    if span.span_type not in ("model", "tool"):
        return ""
    attrs = span.attributes or {}
    status_tag = "" if span.status == "ok" else f" [{span.status}]"
    header = (
        f"┌─ [{span.span_type}] {span.name}  "
        f"{span.duration_ms:.1f}ms{status_tag}  "
        f"trace={span.trace_id[:8]} span={span.span_id[:8]}"
    )
    lines: list[str] = [header]

    if span.span_type == "model":
        msgs = attrs.get("input") or []
        lines.append(f"│  input ({len(msgs)} msgs):")
        for m in msgs:
            role = m.get("role", "?")
            content = _truncate(m.get("content", ""), max_line)
            lines.append(f"│    [{role}] {content}")
        out = attrs.get("output") or {}
        if isinstance(out, dict):
            content = _truncate(out.get("content", ""), max_line)
            lines.append(f"│  output: {content}")
            if out.get("tool_calls"):
                lines.append(f"│  tool_calls: {_truncate(out['tool_calls'], max_line)}")
            if out.get("usage"):
                lines.append(f"│  usage: {_truncate(out['usage'], max_line)}")
        else:
            lines.append(f"│  output: {_truncate(out, max_line)}")
    else:  # tool
        lines.append(f"│  input: {_truncate(attrs.get('input'), max_line)}")
        lines.append(f"│  output: {_truncate(attrs.get('output'), max_line)}")

    if span.error:
        lines.append(f"│  error: {_truncate(span.error, max_line)}")
    lines.append("└─")
    return "\n".join(lines)
