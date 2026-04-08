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
    # 是否让 LLM HTTP 客户端读取系统代理环境变量（HTTP_PROXY / HTTPS_PROXY / NO_PROXY）。
    # False 时强制直连，忽略系统代理；True 时遵循环境变量。
    use_system_proxy: bool = False

    model_config = {"populate_by_name": True, "env_prefix": "LLM_"}


class Neo4jSettings(BaseSettings):
    """Neo4j 图数据库配置。"""

    # 总开关：关闭后服务启动不依赖 Neo4j，相关功能短路返回，不影响其他业务。
    enabled: bool = False
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

    # Embedding Provider 配置
    embedding_provider: str = "default"  # default | fake | openai | dashscope | zhipu
    embedding_api_key: str = Field(default="", alias="CHROMA_EMBEDDING_API_KEY")
    embedding_api_base: str = ""

    # 多 Collection 划分（按知识类型）。键为业务标识，值为 collection name。
    collections: dict[str, str] = Field(
        default_factory=lambda: {
            "ontology_p2p": "ontology_p2p",
            "business_docs_p2p": "business_docs_p2p",
            "analysis_reports": "analysis_reports",
            "memory_long_term": "memory_long_term",
        }
    )

    # 检索默认值
    default_top_k: int = 5
    min_score: float = 0.0  # 0 表示不过滤

    # 是否在长期记忆 / 报告写入时同步索引到向量库（默认关闭，按需启用）
    enable_long_term_indexing: bool = False
    enable_report_indexing: bool = False

    model_config = {"populate_by_name": True, "env_prefix": "CHROMA_"}

    def collection_for(self, key: str) -> str:
        """根据业务键获取实际 Chroma collection 名称，未配置时回退为键本身。"""
        return self.collections.get(key, key)


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


class MockDataSettings(BaseSettings):
    """模拟数据配置。"""

    record_count: int = 500
    seed: int = 42

    model_config = {"env_prefix": "MOCK_DATA_"}


class AnalysisSettings(BaseSettings):
    """分析任务配置。"""

    default_time_range_days: int = 30
    max_time_range_days: int = 365
    # Agent + LLM 首次冷启动可能数十秒，5s 过短，调到 60s
    response_timeout_seconds: float = 60.0
    # 数据库 I/O 在线程池里执行，单次操作的硬超时
    db_io_timeout_seconds: float = 30.0

    model_config = {"env_prefix": "ANALYSIS_"}


class AgentRuntimeSettings(BaseSettings):
    """Agent 运行期资源配置。"""

    # 进程内最大并发 session 数（LRU 淘汰）
    max_short_term_sessions: int = 1024
    # 重试退避基数（秒），实际等待 base * 2**attempt
    retry_backoff_base_seconds: float = 1.0
    retry_backoff_max_seconds: float = 30.0

    model_config = {"env_prefix": "AGENT_"}


class ObservabilitySettings(BaseSettings):
    """可观测性配置。"""

    max_io_text: int = 2000
    trace_queue_maxsize: int = 10000
    trace_batch_size: int = 50
    trace_flush_interval: float = 1.0

    model_config = {"env_prefix": "OBS_"}


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
    mock_data: MockDataSettings = Field(default_factory=MockDataSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    agent_runtime: AgentRuntimeSettings = Field(default_factory=AgentRuntimeSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    p2p: P2PSettings = Field(default_factory=P2PSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    model_config = {"env_prefix": "APP_", "env_file": ".env", "extra": "ignore"}

    @classmethod
    def from_yaml(cls, yaml_path: Path | None = None) -> "Settings":
        """从 YAML 文件加载配置并与环境变量合并。

        逻辑：
        1. 加载 ``.env`` 中的环境变量（不覆盖已有）。
        2. 读取 YAML，做两处特化映射：
           - ``app.*`` 的字段提升到顶层（``name`` → ``app_name`` 等）；
           - ``p2p.supplier_performance.benchmarks`` / ``memory.short_term`` /
             ``memory.long_term`` 这几个嵌套块需要扁平化。
        3. 把敏感字段从环境变量注入到对应子配置。
        4. 整体丢给 Pydantic 完成校验和子模型构造，避免逐字段手工 ``X(**raw)``。
        """
        from dotenv import load_dotenv

        load_dotenv(_CONFIG_DIR.parent / ".env", override=False)

        raw = _load_yaml(yaml_path or _DEFAULT_CONFIG)

        # app.* → 顶层
        app_data = raw.pop("app", {}) or {}
        merged: dict[str, Any] = {
            "app_name": app_data.get("name", "ERP Analysis Agent"),
            "app_version": app_data.get("version", "0.1.0"),
            "debug": app_data.get("debug", False),
            "language": app_data.get("language", "zh"),
            **raw,
        }

        # p2p.supplier_performance.benchmarks → p2p.supplier_performance
        p2p_raw = merged.get("p2p") or {}
        sp = (p2p_raw.get("supplier_performance") or {}).get("benchmarks")
        if sp is not None:
            p2p_raw["supplier_performance"] = sp
            merged["p2p"] = p2p_raw

        # memory.{short_term,long_term} → MemorySettings 的扁平字段
        mem_raw = merged.get("memory") or {}
        if "short_term" in mem_raw or "long_term" in mem_raw:
            short_term = mem_raw.get("short_term") or {}
            long_term = mem_raw.get("long_term") or {}
            merged["memory"] = {
                "short_term_max_messages": short_term.get("max_messages", 20),
                "short_term_summary_threshold": short_term.get("summary_threshold", 15),
                "long_term_max_retrieved": long_term.get("max_retrieved", 5),
            }

        # 环境变量注入敏感字段
        for section, env_key, field in (
            ("llm", "LLM_API_KEY", "api_key"),
            ("neo4j", "NEO4J_PASSWORD", "password"),
            ("postgresql", "POSTGRES_PASSWORD", "password"),
        ):
            sec = merged.setdefault(section, {}) or {}
            sec[field] = os.getenv(env_key, sec.get(field, ""))
            merged[section] = sec

        return cls(**merged)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例（带缓存）。"""
    return Settings.from_yaml()
