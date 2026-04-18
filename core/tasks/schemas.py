"""异步分析任务相关的 Pydantic / 枚举模型。

本模块的数据类只用于 API 响应体和进程内事件载荷，
不参与 ORM 持久化；持久化复用现有 ``trace_runs`` / ``chat_messages`` 表。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from api.schemas.domain import AnalysisResult, ErrorInfo
from core.time_utils import now_cn


class TaskState(str, Enum):
    """异步分析任务的生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    ABORTED = "aborted"


TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.OK, TaskState.ERROR, TaskState.ABORTED}
)


class AnalysisTaskAck(BaseModel):
    """``POST /analyze/async`` 的响应体。"""

    trace_id: str = Field(description="任务 trace ID，全链路唯一")
    status: TaskState = Field(description="任务初始状态（通常为 queued）")
    session_id: str = Field(description="会话 ID")
    user_id: str = Field(description="用户 ID")
    user_message_id: str | None = Field(
        default=None, description="auto_persist=True 时返回，前端渲染 user 气泡用"
    )
    assistant_message_id: str | None = Field(
        default=None, description="auto_persist=True 时返回，前端渲染 pending 气泡用"
    )
    poll_url: str = Field(description="查询快照的 URL")
    stream_url: str = Field(description="订阅 SSE 事件流的 URL")
    created_at: datetime = Field(default_factory=now_cn, description="任务创建时间")


class AnalysisTaskSnapshot(BaseModel):
    """``GET /analyze/tasks/{trace_id}`` 的响应体。"""

    trace_id: str
    status: TaskState
    session_id: str | None = None
    user_id: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    stage: str | None = Field(
        default=None, description="当前阶段名（running 时可能有值）"
    )
    result: AnalysisResult | None = Field(
        default=None, description="status=ok 时有值；完成后 TTL 内内存缓存可查"
    )
    error: ErrorInfo | None = Field(
        default=None, description="status=error/aborted 时有值"
    )


# ---------------------------------------------------------------------------
# 事件载荷（作为 dict 在 EventBus 内传递；定义模型只为方便理解 / 生成文档）
# ---------------------------------------------------------------------------


class BaseEvent(BaseModel):
    """SSE 事件统一基础字段。"""

    type: str
    trace_id: str
    ts: datetime = Field(default_factory=now_cn)
    seq: int = Field(description="单调递增序列号，断线重连去重用")


class StatusEvent(BaseEvent):
    type: str = "status"
    state: TaskState


class StageEvent(BaseEvent):
    type: str = "stage"
    name: str
    attrs: dict[str, Any] | None = None


class ToolEvent(BaseEvent):
    type: str = "tool"
    action: str  # "start" | "end"
    name: str
    duration_ms: float | None = None
    status: str | None = None


class DagTaskEvent(BaseEvent):
    type: str = "dag_task"
    action: str
    task_name: str
    duration_ms: float | None = None
    status: str | None = None


class ReportEvent(BaseEvent):
    type: str = "report"
    anomaly_count: int
    duration_ms: float | None = None


class HeartbeatEvent(BaseEvent):
    type: str = "heartbeat"


class DoneEvent(BaseEvent):
    type: str = "done"
    status: TaskState
    duration_ms: float | None = None
    anomaly_count: int | None = None
    error: ErrorInfo | None = None


class ChunkEvent(BaseEvent):
    """LLM 流式输出的 token 级 / micro-batch 级增量事件。

    协议约定（见 docs/sse_issue.md §6）：

    - ``seq`` 固定 0：不参与全局 seq 序列，不入环形缓冲，不参与 Last-Event-ID 重放；
      语义上与 ``heartbeat`` 一致，属于"体验增强型"轻量事件。
    - ``delta`` 是增量文本，不是累计；前端直接 append。
    - ``index`` 在同一 ``message_id`` 内单调递增；若前端观察到 ``index`` 回退到 0
      且 ``lastChunkIndex > 0``，视为后端 tenacity 重试，须清空累加 buffer。
    - ``eos=True`` 表示该 message 的最后一帧；通常 ``delta`` 为空串。
    - 最终一致性由 ``done`` 事件后的 ``GET /analyze/tasks/{trace_id}`` 快照保证：
      前端用 ``result.report_markdown`` 覆盖累加 buffer，避免 chunk 丢失导致残缺。
    """

    type: str = "chunk"
    #: 生成节点标识。Phase 1 仅 "report"（ReportAgent 最终 Markdown）；
    #: Phase 2 可能引入 "agent_final"（ReAct 兜底路径的最终消息）。
    node: str
    #: 对应 AnalysisTaskAck.assistant_message_id（转字符串），前端用来绑定 pending 气泡。
    message_id: str
    #: 增量文本，直接 append 到 chunkBuffer。
    delta: str
    #: 单 message 内 0-based 序号，调试 + 检测后端重试重置。
    index: int
    #: end-of-stream 标记，默认 False。
    eos: bool = False
