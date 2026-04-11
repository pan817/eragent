"""
分析路由模块。

提供分析请求提交、报告查询等 REST API 端点。
采用模块级变量延迟初始化 Orchestrator 和 LongTermMemory，
避免应用启动时就连接数据库等外部资源。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from api.schemas.analysis import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    ErrorInfo,
)
from config.settings import get_settings
from core.chat import get_chat_repository
from core.logging_utils import get_logger
from core.memory import get_long_term_memory
from core.orchestrator.orchestrator import Orchestrator

_logger = get_logger(__name__)


async def _run_db_io(func, /, *args, **kwargs):
    """在线程池中执行同步 DB 调用，附带硬超时，避免阻塞 event loop。"""
    timeout = get_settings().analysis.db_io_timeout_seconds
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        _logger.error("db io timeout after %.1fs: %s", timeout, func.__name__)
        raise HTTPException(
            status_code=504, detail=f"数据库操作超时: {func.__name__}"
        ) from exc

router = APIRouter(tags=["analysis"])

# ---------------------------------------------------------------------------
# 模块级延迟初始化实例
# ---------------------------------------------------------------------------

_orchestrator: Orchestrator | None = None


def _get_orchestrator() -> Orchestrator:
    """获取 Orchestrator 单例（延迟初始化）。"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


# ---------------------------------------------------------------------------
# 路由端点
# ---------------------------------------------------------------------------


@router.post("/analyze", response_model=AnalysisResult)
async def analyze(request: AnalysisRequest) -> AnalysisResult:
    """提交分析请求并返回分析结果。

    接收自然语言查询，由 Orchestrator 编排意图解析和 Agent 执行，
    返回结构化分析结果。如果请求中未指定 session_id 则自动生成。
    当 auto_persist=True 时，自动将 user/assistant 消息写入 chat 表。

    Args:
        request: 分析请求对象，包含查询文本、用户信息等。

    Returns:
        包含异常记录、KPI 报告、Markdown 报告等内容的分析结果。
    """
    try:
        # 自动生成 session_id
        if not request.session_id:
            request = request.model_copy(
                update={"session_id": str(uuid.uuid4())}
            )

        # --- 会话持久化前置：确保 session 存在 ---
        chat_repo = get_chat_repository()
        if request.auto_persist and chat_repo:
            await _ensure_session(chat_repo, request.user_id, request.session_id)

        orchestrator = _get_orchestrator()
        result: AnalysisResult = await orchestrator.analyze(request)

        # --- 会话持久化后置：落库消息 ---
        if request.auto_persist and chat_repo:
            await _persist_messages(chat_repo, request, result)

        return result

    except Exception as exc:
        error_result = AnalysisResult(
            report_id=str(uuid.uuid4()),
            status=AnalysisStatus.FAILED,
            analysis_type=request.analysis_type or AnalysisType.COMPREHENSIVE,
            query=request.query,
            user_id=request.user_id,
            session_id=request.session_id or str(uuid.uuid4()),
            time_range="",
            error=ErrorInfo(
                code="API_ERROR",
                message=str(exc),
            ),
        )
        # 失败也落库（status=error）
        chat_repo = get_chat_repository()
        if request.auto_persist and chat_repo:
            try:
                await _persist_messages(chat_repo, request, error_result)
            except Exception as persist_exc:
                _logger.warning("failed to persist error messages: %s", persist_exc)
        return error_result


async def _ensure_session(
    repo: Any, user_id: str, session_id: str
) -> None:
    """确保 chat_session 存在，不存在则自动创建。"""
    existing = await _run_db_io(repo.get_session, user_id, session_id)
    if existing is None:
        await _run_db_io(repo.create_session_with_id, user_id, session_id)


async def _persist_messages(
    repo: Any,
    request: AnalysisRequest,
    result: AnalysisResult,
) -> None:
    """将 user + assistant 消息落库到 chat_messages。"""
    user_id = request.user_id
    session_id = result.session_id

    if request.regenerate_of:
        # 重新生成模式：更新已有的 assistant 消息
        content = result.report_markdown or (
            result.error.message if result.error else ""
        )
        status = "error" if result.status == AnalysisStatus.FAILED else "success"
        updates = {
            "content": content,
            "status": status,
            "duration_ms": int(result.duration_ms) if result.duration_ms else None,
            "trace_id": result.trace_id or None,
        }
        updated = await _run_db_io(
            repo.update_message, user_id, session_id,
            request.regenerate_of, updates,
        )
        if updated:
            result.assistant_message_id = request.regenerate_of
    else:
        # 正常模式：追加 user + assistant 两条消息
        user_msg = {
            "role": "user",
            "content": request.query,
            "client_id": request.client_user_message_id,
            "status": "success",
            "metadata": request.metadata,
        }
        asst_content = result.report_markdown or (
            result.error.message if result.error else ""
        )
        asst_status = "error" if result.status == AnalysisStatus.FAILED else "success"
        asst_msg = {
            "role": "assistant",
            "content": asst_content or "(无内容)",
            "client_id": request.client_assistant_message_id,
            "status": asst_status,
            "duration_ms": int(result.duration_ms) if result.duration_ms else None,
            "trace_id": result.trace_id or None,
            "metadata": request.metadata,
        }

        append_result = await _run_db_io(
            repo.append_messages, user_id, session_id, [user_msg, asst_msg]
        )

        if append_result:
            msgs = append_result["messages"]
            result.user_message_id = msgs[0]["id"] if len(msgs) > 0 else None
            result.assistant_message_id = msgs[1]["id"] if len(msgs) > 1 else None
            result.session = append_result.get("session")


@router.get("/reports/{report_id}")
async def get_report(report_id: str) -> dict[str, Any]:
    """按 ID 获取单份分析报告。

    从 LongTermMemory 中检索指定 report_id 的报告。
    如果报告不存在则返回 404 错误。

    Args:
        report_id: 报告的 UUID 字符串。

    Returns:
        报告详情字典，包含所有字段。

    Raises:
        HTTPException: 当指定 report_id 的报告不存在时抛出 404。
    """
    memory = get_long_term_memory()
    # 同步 SQLAlchemy 在 async 路由里走线程池 + 硬超时，避免阻塞 event loop
    report: dict[str, Any] | None = await _run_db_io(memory.get_report, report_id)
    if report is None:
        raise HTTPException(
            status_code=404,
            detail=f"报告 {report_id} 不存在",
        )
    return report


@router.get("/reports")
async def list_reports(
    user_id: str = Query(default="default", description="用户 ID"),
    limit: int = Query(default=20, ge=1, le=100, description="返回数量上限"),
) -> list[dict[str, Any]]:
    """列出用户的分析报告。

    按创建时间倒序返回指定用户的报告列表。

    Args:
        user_id: 用户唯一标识，默认为 ``"default"``。
        limit: 返回的最大记录数，默认 20，范围 1-100。

    Returns:
        报告记录列表，每条记录为字典格式。
    """
    memory = get_long_term_memory()
    reports: list[dict[str, Any]] = await _run_db_io(
        memory.list_reports, user_id, limit
    )
    return reports


# ---------------------------------------------------------------------------
# 记忆清理端点
# ---------------------------------------------------------------------------


@router.delete("/memory/short-term", status_code=status.HTTP_200_OK)
async def clear_short_term_memory(
    session_id: str | None = Query(
        default=None,
        description="会话 ID；省略则清空所有会话的短期记忆",
    ),
) -> dict[str, Any]:
    """清理 P2P Agent 的短期记忆（进程内对话上下文）。

    - 指定 ``session_id``：仅清理该会话。
    - 省略 ``session_id``：清空所有会话。
    """
    orchestrator = _get_orchestrator()
    cleared = orchestrator.clear_short_term_memory(session_id)
    return {
        "scope": "session" if session_id else "all",
        "session_id": session_id,
        "cleared_sessions": cleared,
    }


@router.delete("/memory/long-term", status_code=status.HTTP_200_OK)
async def clear_long_term_memory(
    user_id: str | None = Query(
        default=None,
        description="用户 ID；省略且 all=true 时清空全部数据",
    ),
    delete_memories: bool = Query(default=True, description="是否删除 memories 表"),
    delete_reports: bool = Query(default=True, description="是否删除 reports 表"),
    all: bool = Query(
        default=False,
        description="为 true 且未提供 user_id 时，清空所有用户的数据",
    ),
) -> dict[str, Any]:
    """清理长期记忆。

    安全策略：必须显式提供 ``user_id`` 或显式设置 ``all=true``，
    避免误调导致全表清空。
    """
    if not user_id and not all:
        raise HTTPException(
            status_code=400,
            detail="必须提供 user_id，或显式设置 all=true 以清空全部数据",
        )
    if not delete_memories and not delete_reports:
        raise HTTPException(
            status_code=400,
            detail="delete_memories 与 delete_reports 不能同时为 false",
        )

    memory = get_long_term_memory()
    if user_id:
        deleted = await _run_db_io(
            memory.delete_user_data,
            user_id,
            delete_memories=delete_memories,
            delete_reports=delete_reports,
        )
        return {"scope": "user", "user_id": user_id, "deleted": deleted}

    # all=true 且未提供 user_id
    deleted = await _run_db_io(memory.delete_all)
    return {"scope": "all", "deleted": deleted}
