"""会话历史 API 请求/响应 Pydantic 模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ============================================================
# 会话模型
# ============================================================


class SessionOut(BaseModel):
    """会话概览（列表/详情通用）。"""

    id: str
    user_id: str
    title: str
    title_auto: bool
    message_count: int
    last_message_preview: str | None
    created_at: str
    updated_at: str


class MessageOut(BaseModel):
    """消息输出。"""

    id: str
    client_id: str | None = None
    session_id: str
    role: str
    content: str
    status: str
    duration_ms: int | None = None
    trace_id: str | None = None
    created_at: str
    metadata: dict[str, Any] | None = None


# ============================================================
# 请求模型
# ============================================================


class CreateSessionRequest(BaseModel):
    """创建会话请求。"""

    title: str = Field(default="新对话", max_length=120)


class UpdateTitleRequest(BaseModel):
    """更新标题请求。"""

    title: str = Field(..., max_length=80)


class ClearAllRequest(BaseModel):
    """清空全部会话请求。"""

    confirm: str = Field(..., description="必须为 'DELETE_ALL'")


class MessageInput(BaseModel):
    """追加消息的单条输入。"""

    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., min_length=1, max_length=32768)
    client_id: str | None = None
    status: str = Field(default="success", pattern="^(sending|success|error)$")
    duration_ms: int | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] | None = None


class AppendMessagesRequest(BaseModel):
    """追加消息请求。"""

    messages: list[MessageInput] = Field(..., min_length=1)


class UpdateMessageRequest(BaseModel):
    """更新消息请求（所有字段可选）。"""

    content: str | None = Field(default=None, max_length=32768)
    status: str | None = Field(default=None, pattern="^(sending|success|error)$")
    duration_ms: int | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] | None = None


# ============================================================
# 响应模型
# ============================================================


class SessionListResponse(BaseModel):
    """列出会话响应。"""

    sessions: list[SessionOut]
    next_cursor: str | None
    total: int


class SessionCreateResponse(BaseModel):
    """创建会话响应。"""

    session: SessionOut


class SessionDetailResponse(BaseModel):
    """会话详情响应（含消息）。"""

    session: SessionOut
    messages: list[MessageOut]
    has_more_messages: bool


class ClearAllResponse(BaseModel):
    """清空全部响应。"""

    deleted_count: int


class AppendMessagesResponse(BaseModel):
    """追加消息响应。"""

    messages: list[MessageOut]
    session: SessionOut


class SearchSessionsResponse(BaseModel):
    """搜索会话响应。"""

    sessions: list[SessionOut]


# ============================================================
# 统一错误响应
# ============================================================


class ErrorDetail(BaseModel):
    """错误详情。"""

    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    """统一错误响应。"""

    error: ErrorDetail
