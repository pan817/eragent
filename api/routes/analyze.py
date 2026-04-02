"""
分析路由模块。

提供分析请求提交、报告查询等 REST API 端点。
采用模块级变量延迟初始化 Orchestrator 和 LongTermMemory，
避免应用启动时就连接数据库等外部资源。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from api.schemas.analysis import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    ErrorInfo,
)
from config.settings import get_settings
from core.memory import LongTermMemory
from core.orchestrator.orchestrator import Orchestrator

router = APIRouter(tags=["analysis"])

# ---------------------------------------------------------------------------
# 模块级延迟初始化实例
# ---------------------------------------------------------------------------

_orchestrator: Orchestrator | None = None
_long_term_memory: LongTermMemory | None = None


def _get_orchestrator() -> Orchestrator:
    """获取 Orchestrator 单例（延迟初始化）。

    首次调用时创建 Orchestrator 实例，后续调用直接复用。

    Returns:
        Orchestrator 实例。
    """
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


def _get_long_term_memory() -> LongTermMemory:
    """获取 LongTermMemory 单例（延迟初始化）。

    首次调用时根据配置创建 LongTermMemory 实例并初始化表结构，
    后续调用直接复用。

    Returns:
        LongTermMemory 实例。
    """
    global _long_term_memory
    if _long_term_memory is None:
        settings = get_settings()
        _long_term_memory = LongTermMemory(dsn=settings.postgresql.dsn)
        _long_term_memory.init_tables()
    return _long_term_memory


# ---------------------------------------------------------------------------
# 路由端点
# ---------------------------------------------------------------------------


@router.post("/analyze", response_model=AnalysisResult)
async def analyze(request: AnalysisRequest) -> AnalysisResult:
    """提交分析请求并返回分析结果。

    接收自然语言查询，由 Orchestrator 编排意图解析和 Agent 执行，
    返回结构化分析结果。如果请求中未指定 session_id 则自动生成。

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

        orchestrator = _get_orchestrator()
        result: AnalysisResult = await orchestrator.analyze(request)
        return result

    except Exception as exc:
        return AnalysisResult(
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
    memory = _get_long_term_memory()
    report: dict[str, Any] | None = memory.get_report(report_id)
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
    memory = _get_long_term_memory()
    reports: list[dict[str, Any]] = memory.list_reports(
        user_id=user_id,
        limit=limit,
    )
    return reports
