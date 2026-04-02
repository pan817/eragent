"""
系统配置管理模块。

使用 config.yaml 定义结构化配置，Pydantic Settings 做类型校验和环境变量覆盖。
敏感信息（API Key 等）通过环境变量注入，不存储在 yaml 文件中。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


# 配置文件路径（支持多环境）
_CONFIG_DIR = Path(__file__).parent
_DEFAULT_CONFIG = _CONFIG_DIR / "config.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    """加载 YAML 配置文件。"""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class LLMSettings(BaseSettings):
    """LLM 模型配置。"""

    provider: str = "zhipu"
    model: str = "glm-4"
    api_base: str = "https://open.bigmodel.cn/api/paas/v4"
    api_key: str = Field(default="", alias="LLM_API_KEY")
    temperature: float = 0.1
    max_tokens: int = 4096
    timeout: int = 30
    max_retries: int = 3

    model_config = {"populate_by_name": True, "env_prefix": "LLM_"}


class Neo4jSettings(BaseSettings):
    """Neo4j 图数据库配置。"""

    uri: str = "bolt://localhost:7687"
    username: str = "neo4j"
    password: str = Field(default="", alias="NEO4J_PASSWORD")
    database: str = "neo4j"
    max_connection_pool_size: int = 10

    model_config = {"populate_by_name": True, "env_prefix": "NEO4J_"}


class ChromaSettings(BaseSettings):
    """Chroma 向量数据库配置。"""

    persist_directory: str = "./data/chroma"
    collection_name: str = "erp_ontology"
    embedding_model: str = "text-embedding-3-small"

    model_config = {"env_prefix": "CHROMA_"}


class PostgreSQLSettings(BaseSettings):
    """PostgreSQL 数据库配置。"""

    host: str = "localhost"
    port: int = 5432
    database: str = "eragent"
    username: str = "postgres"
    password: str = Field(default="", alias="POSTGRES_PASSWORD")
    pool_size: int = 5
    max_overflow: int = 10

    model_config = {"populate_by_name": True, "env_prefix": "POSTGRES_"}

    @property
    def dsn(self) -> str:
        """生成 PostgreSQL DSN 连接字符串。"""
        return (
            f"postgresql+psycopg2://{self.username}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )


class AnalysisSettings(BaseSettings):
    """分析任务配置。"""

    default_time_range_days: int = 30
    max_time_range_days: int = 365
    response_timeout_seconds: float = 5.0

    model_config = {"env_prefix": "ANALYSIS_"}


class ThreeWayMatchSettings(BaseSettings):
    """三路匹配容差配置。"""

    default_tolerance_pct: float = 5.0
    max_tolerance_pct: float = 10.0
    supplier_tolerances: dict[str, float] = Field(default_factory=dict)
    category_tolerances: dict[str, float] = Field(default_factory=dict)
    amount_thresholds: list[dict[str, Any]] = Field(default_factory=list)

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

    model_config = {"env_prefix": "P2P_"}


class MemorySettings(BaseSettings):
    """记忆管理配置。"""

    short_term_max_messages: int = 20
    short_term_summary_threshold: int = 15
    long_term_max_retrieved: int = 5

    model_config = {"env_prefix": "MEMORY_"}


class LoggingSettings(BaseSettings):
    """日志配置。"""

    level: str = "INFO"
    format: str = "json"
    include_trace_id: bool = True

    model_config = {"env_prefix": "LOG_"}

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: str) -> str:
        """校验日志级别有效性。"""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid_levels:
            raise ValueError(f"日志级别必须是 {valid_levels} 之一，当前值: {v}")
        return v.upper()


class Settings(BaseSettings):
    """系统全局配置，聚合所有子配置模块。"""

    app_name: str = "ERP Analysis Agent"
    app_version: str = "0.1.0"
    debug: bool = False
    language: str = "zh"

    llm: LLMSettings = Field(default_factory=LLMSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    chroma: ChromaSettings = Field(default_factory=ChromaSettings)
    postgresql: PostgreSQLSettings = Field(default_factory=PostgreSQLSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    p2p: P2PSettings = Field(default_factory=P2PSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    model_config = {"env_prefix": "APP_", "env_file": ".env", "extra": "ignore"}

    @classmethod
    def from_yaml(cls, yaml_path: Path | None = None) -> "Settings":
        """从 YAML 文件加载配置并与环境变量合并。"""
        from dotenv import load_dotenv

        # 加载 .env 文件中的环境变量（不覆盖已存在的）
        _env_file = _CONFIG_DIR.parent / ".env"
        load_dotenv(_env_file, override=False)

        path = yaml_path or _DEFAULT_CONFIG
        raw = _load_yaml(path)

        # 从 YAML 扁平化提取各子配置
        llm_data = raw.get("llm", {})
        llm_data["api_key"] = os.getenv("LLM_API_KEY", llm_data.get("api_key", ""))

        neo4j_data = raw.get("neo4j", {})
        neo4j_data["password"] = os.getenv(
            "NEO4J_PASSWORD", neo4j_data.get("password", "")
        )

        pg_data = raw.get("postgresql", {})
        pg_data["password"] = os.getenv(
            "POSTGRES_PASSWORD", pg_data.get("password", "")
        )

        p2p_raw = raw.get("p2p", {})
        p2p_data = P2PSettings(
            three_way_match=ThreeWayMatchSettings(
                **p2p_raw.get("three_way_match", {})
            ),
            payment_compliance=PaymentComplianceSettings(
                **p2p_raw.get("payment_compliance", {})
            ),
            supplier_performance=SupplierPerformanceBenchmarks(
                **p2p_raw.get("supplier_performance", {}).get("benchmarks", {})
            ),
            anomaly_severity=AnomalySeveritySettings(
                **p2p_raw.get("anomaly_severity", {})
            ),
        )

        memory_raw = raw.get("memory", {})
        short_term = memory_raw.get("short_term", {})
        long_term = memory_raw.get("long_term", {})
        memory_data = MemorySettings(
            short_term_max_messages=short_term.get("max_messages", 20),
            short_term_summary_threshold=short_term.get("summary_threshold", 15),
            long_term_max_retrieved=long_term.get("max_retrieved", 5),
        )

        app_data = raw.get("app", {})
        return cls(
            app_name=app_data.get("name", "ERP Analysis Agent"),
            app_version=app_data.get("version", "0.1.0"),
            debug=app_data.get("debug", False),
            language=app_data.get("language", "zh"),
            llm=LLMSettings(**llm_data),
            neo4j=Neo4jSettings(**neo4j_data),
            chroma=ChromaSettings(**raw.get("chroma", {})),
            postgresql=PostgreSQLSettings(**pg_data),
            analysis=AnalysisSettings(**raw.get("analysis", {})),
            p2p=p2p_data,
            memory=memory_data,
            logging=LoggingSettings(**raw.get("logging", {})),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例（带缓存）。"""
    return Settings.from_yaml()
