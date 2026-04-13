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


def _strip_env_overrides(
    data: dict[str, Any],
    model_cls: type[BaseSettings],
) -> dict[str, Any]:
    """从 data 中移除已被环境变量覆盖的字段。

    pydantic-settings 的优先级：init 参数 > 环境变量 > 默认值。
    ``from_yaml`` 把 YAML 值作为 init 参数传入，会压死环境变量。
    本函数在传入前，将已有对应环境变量的字段从 dict 中删除，
    让 pydantic-settings 自动从环境变量读取，确保 .env 为权威源。

    对嵌套 BaseSettings 子模型递归处理。
    """
    if not data:
        return data

    env_prefix = (model_cls.model_config.get("env_prefix") or "").upper()
    result = dict(data)

    for field_name, field_info in model_cls.model_fields.items():
        if field_name not in result:
            continue

        # 判断字段类型是否为嵌套 BaseSettings 子模型
        field_type = field_info.annotation
        if (
            field_type is not None
            and isinstance(field_type, type)
            and issubclass(field_type, BaseSettings)
        ):
            # 嵌套模型：递归处理
            if isinstance(result[field_name], dict):
                result[field_name] = _strip_env_overrides(
                    result[field_name], field_type
                )
        else:
            # 标量字段：环境变量已设置则从 dict 移除
            env_var = f"{env_prefix}{field_name}".upper()
            if env_var in os.environ:
                del result[field_name]

    return result


class LLMSettings(BaseSettings):
    """LLM 模型配置。"""

    provider: str = "qwen"
    model: str = "qwen3-max"
    api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = Field(default="", alias="LLM_API_KEY")
    temperature: float = 0.1
    max_tokens: int = 4096
    timeout: int = 240
    max_retries: int = 2
    # 是否让 LLM HTTP 客户端读取系统代理环境变量（HTTP_PROXY / HTTPS_PROXY / NO_PROXY）。
    # False 时强制直连，忽略系统代理；True 时遵循环境变量。
    use_system_proxy: bool = False
    # 模型上下文窗口大小（token 数），用于 context_budget 占比计算
    context_window: int = 32768
    # 字符数 → token 数的估算比率（中文为主文本约 1.5 字符/token）
    token_estimate_ratio: float = 1.5

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
    username: str = "dev"
    password: str = Field(default="", alias="POSTGRES_PASSWORD")
    pool_size: int = 20
    max_overflow: int = 30

    model_config = {"populate_by_name": True, "env_prefix": "POSTGRES_"}

    @property
    def dsn(self) -> str:
        """生成 SQLAlchemy 使用的 PostgreSQL DSN(psycopg2 驱动)。"""
        return (
            f"postgresql+psycopg2://{self.username}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @property
    def conninfo(self) -> str:
        """psycopg3 / langgraph PostgresSaver 使用的纯 libpq 连接串。"""
        return (
            f"postgresql://{self.username}:{self.password}"
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
    response_timeout_seconds: float = 900.0  # 需覆盖 LLM 含重试最坏情况(240s×3=720s) + 编排开销
    # 数据库 I/O 在线程池里执行，单次操作的硬超时
    db_io_timeout_seconds: float = 30.0
    # 实体编号正则模式（按客户 EBS 编号规则配置）
    entity_patterns: dict[str, list[str]] = Field(default_factory=lambda: {
        "po_number": [r"PO-\d[\da-zA-Z_-]*\d", r"PO-\d+"],
        "supplier_id": [r"SUP-\d+"],
        "invoice_number": [r"INV-\d[\da-zA-Z_-]*\d", r"INV-\d+"],
        "payment_number": [r"PAY-\d[\da-zA-Z_-]*\d", r"PAY-\d+"],
        "receipt_number": [r"RCV-\d[\da-zA-Z_-]*\d", r"RCV-\d+"],
        "days": [r"(?:最近|过去|近)\s*(\d+)\s*天", r"(?:past|last|recent)\s+(\d+)\s*days?"],
    })

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
    short_term_context_trim_enabled: bool = True   # 注入 LLM 前裁剪兜底开关
    short_term_context_max_tokens_pct: int = 15    # 短期记忆最大占 context_window 的百分比
    long_term_enabled: bool = True
    long_term_max_retrieved: int = 5
    long_term_fusion_k: int = 60
    long_term_max_per_user: int = 200  # 每用户保留的最大记忆条数（0 表示不限制）
    long_term_min_content_len: int = 50           # 短内容过滤阈值（0 关闭）
    long_term_dedupe_window_seconds: int = 900    # 内容指纹去重窗口（0 关闭）
    long_term_skip_empty_conclusions: bool = False  # 跳过 anomaly_count=0 且 summary 空的结论
    long_term_context_trim_enabled: bool = True    # 注入 LLM 前裁剪兜底开关
    long_term_context_max_tokens_pct: int = 10     # 长期记忆最大占 context_window 的百分比

    # ReAct 循环内 LLM 输入裁剪（MemoryMiddleware）
    react_trim_enabled: bool = True          # 总开关
    react_keep_recent_rounds: int = 2        # 保留最近几轮完整对话
    react_tool_content_max_chars: int = 500  # 早期 ToolMessage 截断字符数（0=清空）

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
                "short_term_context_trim_enabled": short_term.get("context_trim_enabled", True),
                "short_term_context_max_tokens_pct": short_term.get("context_max_tokens_pct", 15),
                "long_term_enabled": long_term.get("enabled", True),
                "long_term_max_retrieved": long_term.get("max_retrieved", 5),
                "long_term_fusion_k": long_term.get("fusion_k", 60),
                "long_term_max_per_user": long_term.get("max_per_user", 200),
                "long_term_min_content_len": long_term.get("min_content_len", 50),
                "long_term_dedupe_window_seconds": long_term.get("dedupe_window_seconds", 900),
                "long_term_skip_empty_conclusions": long_term.get(
                    "skip_empty_conclusions", False
                ),
                "long_term_context_trim_enabled": long_term.get("context_trim_enabled", True),
                "long_term_context_max_tokens_pct": long_term.get("context_max_tokens_pct", 10),
            }

        # 环境变量优先：移除 YAML dict 中已被环境变量覆盖的字段，
        # 让 pydantic-settings 自动从环境变量读取，确保 .env 为权威源。
        merged = _strip_env_overrides(merged, cls)

        # 环境变量注入敏感字段（支持明文和加密两种模式）
        # 规则：
        #   - 只设明文（FOO=bar）              → 直接使用
        #   - 只设密文（FOO_ENCRYPTED=gAAAA…） → 解密后使用，需提供 ENCRYPTION_MASTER_KEY
        #   - 两者同时设置                    → 启动时报错，配置冲突不允许静默降级
        #   - 均未设置                        → 沿用 YAML 中的值或空字符串（现有行为）
        _master_key: str | None = None  # 懒加载，有密文时才读取

        for section, env_plain, env_enc, field in (
            ("llm",        "LLM_API_KEY",        "LLM_API_KEY_ENCRYPTED",        "api_key"),
            ("neo4j",      "NEO4J_PASSWORD",      "NEO4J_PASSWORD_ENCRYPTED",      "password"),
            ("postgresql", "POSTGRES_PASSWORD",   "POSTGRES_PASSWORD_ENCRYPTED",   "password"),
        ):
            sec = merged.setdefault(section, {}) or {}
            plain_val = os.getenv(env_plain, "")
            enc_val   = os.getenv(env_enc,   "")

            if plain_val and enc_val:
                raise ValueError(
                    f"配置冲突：{env_plain} 和 {env_enc} 不能同时设置。"
                    f"请保留加密版本并删除明文环境变量。"
                )

            if enc_val:
                if _master_key is None:
                    _master_key = os.getenv("ENCRYPTION_MASTER_KEY", "")
                if not _master_key:
                    raise ValueError(
                        f"设置了 {env_enc} 但未提供主密钥 ENCRYPTION_MASTER_KEY。"
                        f"请通过环境变量注入主密钥后重启服务。"
                    )
                from config.crypto import decrypt as _decrypt
                try:
                    sec[field] = _decrypt(enc_val, _master_key)
                except Exception as exc:
                    raise ValueError(
                        f"{env_enc} 解密失败（密钥不匹配或密文损坏）: {exc}"
                    ) from exc
            else:
                sec[field] = plain_val or sec.get(field, "")

            merged[section] = sec

        return cls(**merged)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例（带缓存）。"""
    return Settings.from_yaml()
