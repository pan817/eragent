"""可观测性模块：Agent / Model / Tool 调用链路监控与持久化。"""

from core.observability.checkpointer import attach_tracing as attach_checkpointer_tracing
from core.observability.tracing import (
    TimingMiddleware,
    estimate_tokens,
    record_memory_span,
    record_span,
)
from core.observability.store import (
    TraceStore,
    get_trace_store,
    init_trace_store,
    shutdown_trace_store,
)

# 显式导入 tables 子模块，确保 SQLAlchemy 表注册到 Base.metadata，
# 避免调用方还得加 `from core.observability import tables  # noqa: F401`。
from core.observability import tables as _tables  # noqa: F401

__all__ = [
    "TimingMiddleware",
    "attach_checkpointer_tracing",
    "estimate_tokens",
    "record_memory_span",
    "record_span",
    "TraceStore",
    "get_trace_store",
    "init_trace_store",
    "shutdown_trace_store",
]
