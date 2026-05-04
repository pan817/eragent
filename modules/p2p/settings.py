"""P2P 模块专属配置。

从 ``config/settings.py`` 迁入的 7 个 P2P 业务配置类。
通过 ``get_p2p_settings()`` 从 ``config.yaml`` 的 ``p2p:`` 段加载，
支持环境变量覆盖（各子类的 ``env_prefix``）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings

_CONFIG_YAML = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


class ThreeWayMatchSettings(BaseSettings):
    """三路匹配容差配置。"""

    default_tolerance_pct: float = 5.0
    max_tolerance_pct: float = 10.0
    supplier_tolerances: dict[str, float] = Field(default_factory=dict)
    category_tolerances: dict[str, float] = Field(
        default_factory=lambda: {"RAW_MATERIAL": 10.0}
    )
    amount_thresholds: list[dict[str, Any]] = Field(
        default_factory=lambda: [{"min_amount": 1000000, "tolerance_pct": 2.0}]
    )

    model_config = {"env_prefix": "THREE_WAY_MATCH_"}


class PaymentComplianceSettings(BaseSettings):
    """付款合规性配置。"""

    early_payment_threshold_days: int = 5
    overdue_severity: dict[str, int] = Field(
        default_factory=lambda: {"low_days": 7, "medium_days": 30, "high_days": 30}
    )

    model_config = {"env_prefix": "PAYMENT_COMPLIANCE_"}


class SupplierPerformanceBenchmarks(BaseSettings):
    """供应商绩效 KPI 基准值配置。"""

    otif_rate: float = 95.0
    invoice_accuracy_rate: float = 99.0
    quality_pass_rate: float = 98.0
    price_compliance_rate: float = 98.0

    model_config = {"env_prefix": "SUPPLIER_PERF_"}


class AnomalySeveritySettings(BaseSettings):
    """异常严重等级配置。"""

    high_amount_threshold: float = 500000.0
    variance_high_multiplier: float = 2.0

    model_config = {"env_prefix": "ANOMALY_SEVERITY_"}


class OntologyContextSettings(BaseSettings):
    """本体上下文注入 LLM 时的预算控制。"""

    context_trim_enabled: bool = True  # 注入 LLM 前裁剪兜底开关
    context_max_tokens_pct: int = 8    # 本体上下文最大占 context_window 的百分比

    model_config = {"env_prefix": "ONTOLOGY_"}


class ToolOutputSettings(BaseSettings):
    """P2P tool 返回值裁剪配置。

    ERP 真实数据量级下，单次 tool 调用可能返回几十万字符的 anomalies 列表，
    直接回灌到 ReAct 消息历史或 ReportAgent prompt 会冲爆 context_window。
    在 tool 返回端做源头裁剪：超过 ``max_items`` 的列表只保留前 N 条 + 一条
    "还有 M 条同类结果未列出" 的摘要记录；最终 JSON 再受 ``max_chars`` 兜底。
    """

    max_items: int = 200            # 列表型返回最多保留的条目数（0 关闭）
    max_chars: int = 30000          # 单次 tool 返回 JSON 字符数硬上限（0 关闭）
    query_max_rows: int = 5000      # SQL/Cypher 查询返回行数硬上限（limit=0 或超限时强制截断）

    model_config = {"env_prefix": "P2P_TOOLS_"}


class P2PSettings(BaseSettings):
    """P2P 模块整体配置。"""

    three_way_match: ThreeWayMatchSettings = Field(default_factory=ThreeWayMatchSettings)
    payment_compliance: PaymentComplianceSettings = Field(
        default_factory=PaymentComplianceSettings
    )
    supplier_performance: SupplierPerformanceBenchmarks = Field(
        default_factory=SupplierPerformanceBenchmarks
    )
    anomaly_severity: AnomalySeveritySettings = Field(
        default_factory=AnomalySeveritySettings
    )
    ontology: OntologyContextSettings = Field(
        default_factory=OntologyContextSettings
    )
    tool_output: ToolOutputSettings = Field(default_factory=ToolOutputSettings)

    model_config = {"env_prefix": "P2P_"}


@lru_cache(maxsize=1)
def get_p2p_settings() -> P2PSettings:
    """从 config.yaml 的 ``p2p:`` 段加载 P2P 配置（带缓存）。

    YAML 中 ``p2p.supplier_performance.benchmarks`` 需扁平化为
    ``p2p.supplier_performance``，与 ``SupplierPerformanceBenchmarks``
    字段名对齐。
    """
    raw: dict[str, Any] = {}
    if _CONFIG_YAML.exists():
        with open(_CONFIG_YAML, encoding="utf-8") as f:
            full = yaml.safe_load(f) or {}
        raw = full.get("p2p") or {}

    # p2p.supplier_performance.benchmarks → p2p.supplier_performance
    sp = (raw.get("supplier_performance") or {}).get("benchmarks")
    if sp is not None:
        raw["supplier_performance"] = sp

    return P2PSettings(**raw)
