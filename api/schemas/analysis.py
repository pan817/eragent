"""
HTTP 接口 Pydantic 模型。

AnalysisRequest 为 REST API 入参，包含 HTTP 专属字段（output_mode、auto_persist 等）。
业务领域模型定义在 api.schemas.domain 中，此处 re-export 以保持向后兼容。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# re-export：保持 `from api.schemas.analysis import AnalysisType` 等旧写法可用
from api.schemas.domain import (  # noqa: F401
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    AnomalyDetail,
    AnomalyRecord,
    DocumentRef,
    ErrorInfo,
    KPIStatus,
    KPIValue,
    Severity,
    SupplierKPIReport,
)


# ============================================================
# HTTP 请求模型
# ============================================================

class AnalysisRequest(BaseModel):
    """分析请求。"""

    query: str = Field(..., min_length=1, max_length=2000, description="自然语言分析查询")
    user_id: str = Field(default="default", description="用户 ID")
    session_id: str = Field(default="", description="会话 ID，空则自动生成")
    time_range_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description="分析时间范围（天），不传则使用配置默认值。time_range 优先级更高",
    )
    time_range: str | None = Field(
        default=None,
        pattern=r"^(\d+d|this_month|last_month)$",
        description="时间窗口：7d/30d/90d/this_month/last_month，优先级高于 time_range_days",
    )
    analysis_type: AnalysisType | None = Field(
        default=None,
        description="分析类型，不传则由系统自动识别",
    )
    analyst_role: str = Field(
        default="general",
        pattern="^(general|procurement|finance|supply_chain|audit|management)$",
        description="分析师角色，用于 L3 LLM 分类时提供角色偏好先验",
    )
    output_mode: str = Field(
        default="auto",
        pattern="^(auto|detailed|brief|table|chat)$",
        description=(
            "输出模式："
            "auto=由后端按 intent_kind 自动选（事实查询→chat，分析查询→detailed，**默认推荐**），"
            "detailed=详细报告，brief=简报摘要，table=数据表格，chat=自然对话。"
            "显式传非 auto 值时，**后端尊重前端意图、不做覆盖**；"
            "传 auto 或不传则交由 Orchestrator 根据查询性质决策。"
        ),
    )

    # 会话持久化相关（Section 5）
    auto_persist: bool = Field(
        default=True,
        description="是否自动将 user/assistant 消息写入 chat_sessions/chat_messages",
    )
    regenerate_of: str | None = Field(
        default=None,
        description="若为已有 assistant message_id，本次请求视为对它的重试，更新而非新建",
    )
    client_user_message_id: str | None = Field(
        default=None,
        description="前端乐观渲染的 user 消息临时 ID",
    )
    client_assistant_message_id: str | None = Field(
        default=None,
        description="前端乐观渲染的 assistant 消息临时 ID",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="请求级元信息，写入消息的 metadata 字段",
    )
