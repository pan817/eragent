"""
业务领域 Pydantic 模型。

定义分析业务的核心数据契约：枚举类型、异常记录、KPI 报告、分析结果等。
本模块仅依赖 Pydantic，不依赖 FastAPI，可被 core/modules 安全 import。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from core.time_utils import now_cn


# ============================================================
# 枚举类型
# ============================================================

class AnalysisType(str, Enum):
    """分析类型枚举。"""

    THREE_WAY_MATCH = "three_way_match"
    PRICE_VARIANCE = "price_variance"
    PAYMENT_COMPLIANCE = "payment_compliance"
    SUPPLIER_PERFORMANCE = "supplier_performance"
    SPEND_ANALYSIS = "spend_analysis"
    RECEIPT_ANOMALY = "receipt_anomaly"
    INVOICE_DUPLICATE = "invoice_duplicate"
    DISCOUNT_UTILIZATION = "discount_utilization"
    PO_CYCLE_TIME = "po_cycle_time"
    VENDOR_CONCENTRATION = "vendor_concentration"
    COMPREHENSIVE = "comprehensive"


class Severity(str, Enum):
    """异常严重等级。"""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AnalysisStatus(str, Enum):
    """分析任务执行状态。"""

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class KPIStatus(str, Enum):
    """KPI 状态评级。"""

    GOOD = "GOOD"
    NORMAL = "NORMAL"
    BELOW_TARGET = "BELOW_TARGET"
    CRITICAL = "CRITICAL"


# ============================================================
# 异常记录模型
# ============================================================

class DocumentRef(BaseModel):
    """关联的业务单据引用。"""

    po_number: str = Field(default="", description="采购订单号")
    gr_number: str = Field(default="", description="收货单号")
    invoice_number: str = Field(default="", description="发票号")
    payment_number: str = Field(default="", description="付款单号")
    supplier_name: str = Field(default="", description="供应商名称")


class AnomalyDetail(BaseModel):
    """异常检测明细。"""

    field: str = Field(description="异常字段名称")
    expected_value: float | None = Field(default=None, description="预期值")
    actual_value: float | None = Field(default=None, description="实际值")
    variance_pct: float | None = Field(default=None, description="偏差百分比")
    tolerance_pct: float | None = Field(default=None, description="配置容差百分比")


class AnomalyRecord(BaseModel):
    """异常记录。"""

    anomaly_id: str = Field(description="异常唯一 ID")
    anomaly_type: str = Field(description="异常类型编码")
    severity: Severity = Field(description="严重等级")
    documents: DocumentRef = Field(default_factory=DocumentRef, description="关联单据")
    rule_id: str = Field(description="触发的本体规则 ID")
    details: AnomalyDetail = Field(description="异常明细")
    description: str = Field(description="异常描述（中文）")
    recommended_action: str = Field(description="建议处理动作（中文）")
    detected_at: datetime = Field(default_factory=now_cn, description="检测时间")
    ontology_evidence: str = Field(default="", description="本体规则依据")


# ============================================================
# KPI 模型
# ============================================================

class KPIValue(BaseModel):
    """单项 KPI 值。"""

    value: float = Field(description="KPI 值")
    unit: str = Field(default="%", description="单位")
    benchmark: float = Field(description="基准值")
    status: KPIStatus = Field(description="状态评级")


class SupplierKPIReport(BaseModel):
    """供应商绩效 KPI 报告。"""

    supplier_id: str = Field(description="供应商 ID")
    supplier_name: str = Field(description="供应商名称")
    period: str = Field(description="评估周期")
    kpis: dict[str, KPIValue] = Field(description="各项 KPI 指标")


# ============================================================
# 分析结果模型
# ============================================================

class ErrorInfo(BaseModel):
    """错误信息。"""

    code: str = Field(description="错误码")
    message: str = Field(description="错误描述")
    retry_count: int = Field(default=0, description="已重试次数")


class AnalysisResult(BaseModel):
    """分析结果（结构化 JSON + Markdown 报告混合输出）。"""

    report_id: str = Field(description="报告唯一 ID")
    trace_id: str = Field(default="", description="全链路监控 trace ID，可用于查询 /traces/{trace_id}")
    status: AnalysisStatus = Field(description="执行状态")
    analysis_type: AnalysisType = Field(description="分析类型")
    query: str = Field(description="原始查询")
    user_id: str = Field(description="用户 ID")
    session_id: str = Field(description="会话 ID")
    time_range: str = Field(description="分析时间范围")

    # 结构化结果
    anomalies: list[AnomalyRecord] = Field(default_factory=list, description="异常列表")
    supplier_kpis: list[SupplierKPIReport] = Field(
        default_factory=list, description="供应商 KPI 报告"
    )
    summary: dict[str, Any] = Field(default_factory=dict, description="统计摘要")

    # Markdown 可读报告
    report_markdown: str = Field(default="", description="Markdown 格式报告")

    # 错误信息
    error: ErrorInfo | None = Field(default=None, description="错误信息")
    completed_tasks: list[str] = Field(default_factory=list, description="已完成子任务")
    failed_tasks: list[str] = Field(default_factory=list, description="失败子任务")

    # 元信息
    created_at: datetime = Field(default_factory=now_cn, description="报告生成时间")
    duration_ms: float = Field(default=0.0, description="分析耗时（毫秒）")

    # 会话持久化反馈
    session: dict[str, Any] | None = Field(
        default=None,
        description="持久化后的会话概览（id/title/message_count/updated_at）",
    )
    user_message_id: str | None = Field(
        default=None, description="落库后的 user 消息 ID"
    )
    assistant_message_id: str | None = Field(
        default=None, description="落库后的 assistant 消息 ID"
    )
