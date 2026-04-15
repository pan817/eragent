"""异步任务执行期的 ContextVar 注入点。

用于把 ``assistant_message_id`` 等"仅异步分析任务路径需要的上下文"
透传到深层链路（ReportAgent 流式输出时需要绑定到前端 pending 气泡），
避免 orchestrator / DAG executor / ReportAgent 的 API 签名传染。

注意：``trace_id`` 已由 :mod:`core.observability.middleware._current_trace`
负责维护，本模块不重复定义；调用方通过 ``_current_trace.get().trace_id`` 取用。
"""

from __future__ import annotations

import contextvars

#: 当前异步任务对应的 assistant chat message id（前端用它把 chunk 绑定到气泡）。
#: 仅 ``auto_persist=True`` 且走 /analyze/async 路径时非空；其他场景为 None。
current_assistant_message_id: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("current_assistant_message_id", default=None)
)


def get_current_message_id() -> str | None:
    """读取当前上下文的 assistant_message_id；未注入时返回 None。"""
    return current_assistant_message_id.get()
