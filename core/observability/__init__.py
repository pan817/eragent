"""可观测性模块：Agent / Model / Tool 调用链路监控与持久化。"""

from core.observability.middleware import TimingMiddleware
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
    "TraceStore",
    "get_trace_store",
    "init_trace_store",
    "shutdown_trace_store",
]
