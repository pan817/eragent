"""会话历史路由模块。

提供 9 个 REST 端点，与 analyze 路由完全解耦。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, status

from api.schemas.session import (
    AppendMessagesRequest,
    AppendMessagesResponse,
    ClearAllRequest,
    ClearAllResponse,
    CreateSessionRequest,
    SearchSessionsResponse,
    SessionCreateResponse,
    SessionDetailResponse,
    SessionListResponse,
    SessionOut,
    UpdateMessageRequest,
    UpdateTitleRequest,
)
from config.settings import get_settings
from core.chat.repository import ChatRepository
from core.logging_utils import get_logger

_logger = get_logger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])

# ---------------------------------------------------------------------------
# 模块级 ChatRepository（由 main.py lifespan 初始化）
# ---------------------------------------------------------------------------

_repo: ChatRepository | None = None


def init_chat_repo(repo: ChatRepository) -> None:
    """由 main.py lifespan 调用，注入 ChatRepository 实例。"""
    global _repo
    _repo = repo


def _get_repo() -> ChatRepository:
    if _repo is None:
        raise HTTPException(
            status_code=503, detail="ChatRepository 未初始化"
        )
    return _repo


async def _run_db(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    """在线程池中执行同步 DB 调用。"""
    timeout = get_settings().analysis.db_io_timeout_seconds
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        _logger.error("db io timeout: %s", func.__name__)
        raise HTTPException(status_code=504, detail="数据库操作超时") from exc


def _require_user(x_user_id: str | None) -> str:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="未登录")
    return x_user_id


# ---------------------------------------------------------------------------
# 4.1 列出会话
# ---------------------------------------------------------------------------


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = Query(default=None),
    x_user_id: str | None = Header(default=None),
) -> SessionListResponse:
    user_id = _require_user(x_user_id)
    repo = _get_repo()
    result = await _run_db(repo.list_sessions, user_id, limit, cursor)
    return SessionListResponse(**result)


# ---------------------------------------------------------------------------
# 4.2 创建会话
# ---------------------------------------------------------------------------


@router.post("", response_model=SessionCreateResponse, status_code=201)
async def create_session(
    body: CreateSessionRequest | None = None,
    x_user_id: str | None = Header(default=None),
) -> SessionCreateResponse:
    user_id = _require_user(x_user_id)
    repo = _get_repo()
    title = body.title if body else "新对话"
    session = await _run_db(repo.create_session, user_id, title)
    return SessionCreateResponse(session=SessionOut(**session))


# ---------------------------------------------------------------------------
# 4.9 搜索会话（必须在 /{session_id} 之前，避免 "search" 被当成 session_id）
# ---------------------------------------------------------------------------


@router.get("/search", response_model=SearchSessionsResponse)
async def search_sessions(
    q: str = Query(..., min_length=1, description="搜索关键词"),
    limit: int = Query(default=20, ge=1, le=50),
    scope: str = Query(default="all", pattern="^(title|content|all)$"),
    x_user_id: str | None = Header(default=None),
) -> SearchSessionsResponse:
    user_id = _require_user(x_user_id)
    repo = _get_repo()
    sessions = await _run_db(
        repo.search_sessions, user_id, q.strip(), limit, scope
    )
    return SearchSessionsResponse(
        sessions=[SessionOut(**s) for s in sessions]
    )


# ---------------------------------------------------------------------------
# 4.3 获取会话详情
# ---------------------------------------------------------------------------


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session_detail(
    session_id: str,
    message_limit: int = Query(default=200, ge=1, le=500),
    x_user_id: str | None = Header(default=None),
) -> SessionDetailResponse:
    user_id = _require_user(x_user_id)
    repo = _get_repo()
    result = await _run_db(
        repo.get_session_with_messages, user_id, session_id, message_limit
    )
    if result is None:
        raise HTTPException(status_code=404, detail="会话不存在或已被删除")
    return SessionDetailResponse(**result)


# ---------------------------------------------------------------------------
# 4.4 更新会话标题
# ---------------------------------------------------------------------------


@router.patch("/{session_id}", response_model=SessionCreateResponse)
async def update_session_title(
    session_id: str,
    body: UpdateTitleRequest,
    x_user_id: str | None = Header(default=None),
) -> SessionCreateResponse:
    user_id = _require_user(x_user_id)
    repo = _get_repo()
    session = await _run_db(repo.update_title, user_id, session_id, body.title)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已被删除")
    return SessionCreateResponse(session=SessionOut(**session))


# ---------------------------------------------------------------------------
# 4.5 删除单个会话
# ---------------------------------------------------------------------------


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    x_user_id: str | None = Header(default=None),
) -> None:
    user_id = _require_user(x_user_id)
    repo = _get_repo()
    found = await _run_db(repo.delete_session, user_id, session_id)
    if not found:
        raise HTTPException(status_code=404, detail="会话不存在或已被删除")


# ---------------------------------------------------------------------------
# 4.6 清空用户全部会话
# ---------------------------------------------------------------------------


@router.delete("", response_model=ClearAllResponse)
async def clear_all_sessions(
    body: ClearAllRequest | None = None,
    x_user_id: str | None = Header(default=None),
) -> ClearAllResponse:
    user_id = _require_user(x_user_id)
    if body is None or body.confirm != "DELETE_ALL":
        raise HTTPException(
            status_code=400,
            detail="必须传 confirm='DELETE_ALL' 确认清空操作",
        )
    repo = _get_repo()
    count = await _run_db(repo.delete_all_sessions, user_id)
    return ClearAllResponse(deleted_count=count)


# ---------------------------------------------------------------------------
# 4.7 追加消息
# ---------------------------------------------------------------------------


@router.post(
    "/{session_id}/messages",
    response_model=AppendMessagesResponse,
    status_code=201,
)
async def append_messages(
    session_id: str,
    body: AppendMessagesRequest,
    x_user_id: str | None = Header(default=None),
) -> AppendMessagesResponse:
    user_id = _require_user(x_user_id)
    repo = _get_repo()
    msgs = [m.model_dump() for m in body.messages]
    try:
        result = await _run_db(repo.append_messages, user_id, session_id, msgs)
    except ValueError as exc:
        msg = str(exc)
        if "SESSION_FULL" in msg:
            raise HTTPException(status_code=409, detail=msg) from exc
        if "CONTENT_TOO_LARGE" in msg:
            raise HTTPException(status_code=413, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="会话不存在或已被删除")
    return AppendMessagesResponse(**result)


# ---------------------------------------------------------------------------
# 4.8 更新消息
# ---------------------------------------------------------------------------


@router.patch("/{session_id}/messages/{message_id}")
async def update_message(
    session_id: str,
    message_id: str,
    body: UpdateMessageRequest,
    x_user_id: str | None = Header(default=None),
) -> dict:
    user_id = _require_user(x_user_id)
    repo = _get_repo()
    updates = body.model_dump(exclude_none=True)
    try:
        result = await _run_db(
            repo.update_message, user_id, session_id, message_id, updates
        )
    except ValueError as exc:
        msg = str(exc)
        if "CONTENT_TOO_LARGE" in msg:
            raise HTTPException(status_code=413, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="消息不存在")
    return result
