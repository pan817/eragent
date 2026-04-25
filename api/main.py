"""
FastAPI 应用入口。

创建并配置 FastAPI 应用实例，挂载路由、中间件，
提供健康检查和数据初始化端点。Agent 等重量级组件采用延迟加载策略。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.analyze import router as analyze_router
from api.routes.analyze_async import router as analyze_async_router
from api.routes.etl import router as etl_router
from api.routes.sessions import router as sessions_router
from api.routes.admin_metrics import router as admin_metrics_router
from api.routes.traces import router as traces_router
from config.settings import get_settings
from core.database import (
    create_tables,
    get_engine,
    get_session_factory,
    reset_and_seed,
)
from core.chat import ChatRepository, init_chat_repository
from core.logging_utils import get_logger
from core.observability import init_trace_store, shutdown_trace_store
from core.tasks import (
    init_event_bus,
    init_task_registry,
    shutdown_event_bus,
    shutdown_task_registry,
)
from modules.p2p.schemas import SchemaRegistry
from modules.p2p.tools import set_repository
from modules.p2p.tools._inject import set_graph_schema

_logger = get_logger(__name__)


def _mask_redis_url(url: str) -> str:
    """把 Redis URL 里的密码替换成 ***，用于启动日志打印。"""
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        if parsed.password:
            netloc = parsed.netloc.replace(parsed.password, "***")
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:  # noqa: BLE001
        pass
    return url


def _check_event_backend_matches_workers(event_backend: str) -> None:
    """检查 event_backend 与实际 worker 数量是否匹配；不匹配直接 fail-fast。

    多 worker 部署下 memory backend 会导致 POST 与 SSE 可能落在不同 worker
    进程，订阅方永远收不到发布方的事件（前端 Issue 1 的根因，详见
    docs/issue/async_analyze_backend_issue.md）。

    2026-04 生产复盘决定升级为 RuntimeError：WARNING 容易被忽略，
    一旦用户点击"异步分析"就会表现为"界面转圈 15 分钟后失败"，
    比服务启动不起来更糟糕。压测等临时场景请显式设置
    ASYNC_ANALYSIS_EVENT_BACKEND=redis 再配合 workers>1 使用。
    """
    import os

    workers_env = os.environ.get("WEB_CONCURRENCY") or os.environ.get("WORKERS")
    try:
        workers = int(workers_env) if workers_env else 1
    except ValueError:
        workers = 1
    if workers > 1 and event_backend == "memory":
        raise RuntimeError(
            f"async_analysis.event_backend=memory 与 workers={workers} 不兼容: "
            "多 worker 下 SSE 事件无法跨进程送达,前端将只能收到心跳事件。"
            "请在 config.yaml 设置 async_analysis.event_backend=redis 并提供 redis_url,"
            "或把 workers 降回 1。详见 docs/issue/async_analyze_backend_issue.md"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理。

    启动时仅建表（不灌数据），数据通过 POST /api/v1/init-data 接口触发。
    Agent 等重量级组件由 Orchestrator 延迟加载，不在此处初始化。

    Args:
        app: FastAPI 应用实例。

    Yields:
        None
    """
    _logger.info(
        "lifespan startup: app=%s version=%s debug=%s",
        get_settings().app_name,
        get_settings().app_version,
        get_settings().debug,
    )

    settings = get_settings()
    _logger.info(
        "settings loaded: llm=%s llm_fast=%s timezone=%s",
        settings.llm.model, settings.llm_fast.model, settings.timezone,
    )

    # tiktoken warmup: pre-download BPE encoding during startup to avoid
    # 30-45s stall on the first user request (blocked by network to
    # openaipublic.blob.core.windows.net in restricted environments).
    if settings.tiktoken_warmup_enabled:
        import os
        from pathlib import Path

        cache_dir = str(Path(__file__).resolve().parent.parent / ".tiktoken_cache")
        os.makedirs(cache_dir, exist_ok=True)
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", cache_dir)
        try:
            import tiktoken

            tiktoken.get_encoding("cl100k_base")
            _logger.info("tiktoken warmup ok (cache_dir=%s)", cache_dir)
        except Exception:  # noqa: BLE001
            _logger.warning("tiktoken warmup failed; first LLM init may be slow")

    engine = get_engine(settings.postgresql)
    _logger.info("postgresql engine ready: dsn=%s", settings.postgresql.dsn.split("@")[-1])
    create_tables(engine)
    _logger.info("database migrations applied (alembic upgrade head)")
    session_factory = get_session_factory(engine)
    app.state.settings = settings
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory
    schema_reg = SchemaRegistry.get(settings.erp_schema)
    repo = schema_reg.repository_factory(session_factory)
    set_repository(repo)
    set_graph_schema(schema_reg.graph_schema)
    init_trace_store(session_factory)
    _logger.info("trace store ready (TimingMiddleware observable)")

    # 初始化会话历史 Repository（全局 + 路由模块双注入）
    chat_repo = ChatRepository(session_factory)
    init_chat_repository(chat_repo)
    from api.routes.sessions import init_chat_repo
    init_chat_repo(chat_repo)
    _logger.info("chat repository ready")

    # 初始化异步分析基础设施：EventBus + TaskRegistry
    async_cfg = settings.async_analysis
    _check_event_backend_matches_workers(async_cfg.event_backend)
    bus = init_event_bus(
        buffer_size=async_cfg.event_buffer_size,
        backend=async_cfg.event_backend,
        redis_url=async_cfg.redis_url,
        redis_key_prefix=async_cfg.redis_key_prefix,
    )
    # Redis 后端启动时 fail-fast：连不通 / 认证失败就别让服务起来接流量，
    # 否则会退化成每个 POST /analyze/async 都在 submit 里跑第一条 Redis 命令失败 → 500。
    # 配错 redis_url（比如 `redis://pwd@host` 而不是 `redis://:pwd@host`）是最常见的翻车点。
    if async_cfg.event_backend == "redis" and hasattr(bus, "ping"):
        try:
            bus.ping()
            _logger.info(
                "redis event bus ping ok (url=%s, prefix=%s)",
                _mask_redis_url(async_cfg.redis_url),
                async_cfg.redis_key_prefix,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"RedisEventBus ping failed ({exc!r})；请检查 "
                "ASYNC_ANALYSIS_REDIS_URL（密码格式须为 redis://:<pwd>@host:port/db）"
                "以及 Redis 实例是否可达。"
            ) from exc
    registry = init_task_registry(
        event_bus=bus,
        session_factory=session_factory,
        max_concurrent_tasks=async_cfg.max_concurrent_tasks,
        result_cache_ttl_sec=async_cfg.result_cache_ttl_sec,
        sweep_interval_sec=async_cfg.sweep_interval_sec,
        trace_flush_barrier_timeout=async_cfg.trace_flush_barrier_timeout,
        runner_hard_timeout_seconds=async_cfg.runner_hard_timeout_seconds,
        runner_stall_grace_seconds=async_cfg.runner_stall_grace_seconds,
        orphan_pending_chat_max_age_sec=async_cfg.orphan_pending_chat_max_age_sec,
    )
    # 跨进程残留收敛：把上次进程中残留的 running / pending 标记为 aborted / error
    registry.recover_on_startup()
    registry.start_background()
    _logger.info(
        "task registry ready: backend=%s max_concurrent=%d",
        async_cfg.event_backend, async_cfg.max_concurrent_tasks,
    )
    # 初始化 Graphiti ETL（可选，受 graphiti_etl.enabled 控制）
    etl_scheduler = None
    graphiti_client = None
    if settings.graphiti_etl.enabled and settings.neo4j.enabled:
        from core.etl.client import GraphitiClient
        from core.etl.scheduler import ETLScheduler
        from core.etl.pipeline import ETLPipeline
        from core.etl.state import SyncStateManager
        from core.etl.transformers.structured import StructuredTransformer
        from core.etl.transformers.registry import MAPPING_REGISTRY
        from core.etl.loaders.graphiti_loader import GraphitiLoader
        from core.etl.extractors.master_data import MasterDataExtractor
        from core.etl.extractors.purchasing import PurchasingExtractor
        from core.etl.extractors.receiving import ReceivingExtractor
        from core.etl.extractors.payables import PayablesExtractor
        from core.etl.extractors.sourcing import SourcingExtractor

        etl_cfg = settings.graphiti_etl
        graphiti_client = GraphitiClient(settings)
        try:
            await graphiti_client.initialize()
            _logger.info("Graphiti client connected")
        except Exception:
            _logger.warning("Graphiti client init failed — ETL disabled", exc_info=True)
            graphiti_client = None

        if graphiti_client is not None:
            batch_size = etl_cfg.batch_size
            max_rows = etl_cfg.max_rows_per_table
            ext_kwargs = dict(
                session_factory=session_factory,
                batch_size=batch_size,
                max_rows_per_table=max_rows,
            )
            extractors = {
                "master_data": MasterDataExtractor(**ext_kwargs),
                "purchasing": PurchasingExtractor(**ext_kwargs),
                "receiving": ReceivingExtractor(**ext_kwargs),
                "payables": PayablesExtractor(**ext_kwargs),
                "sourcing": SourcingExtractor(**ext_kwargs),
            }
            transformer = StructuredTransformer(MAPPING_REGISTRY)
            loader = GraphitiLoader(
                graphiti_client,
                episode_batch_size=etl_cfg.episode_batch_size,
                max_concurrency=etl_cfg.max_load_concurrency,
            )
            state_mgr = SyncStateManager(session_factory)
            pipeline = ETLPipeline(
                extractors=extractors,
                transformer=transformer,
                loader=loader,
                state_manager=state_mgr,
                max_concurrent_domains=etl_cfg.max_concurrent_domains,
            )
            etl_scheduler = ETLScheduler(
                pipeline=pipeline,
                state_manager=state_mgr,
                interval_seconds=etl_cfg.sync_interval_seconds,
                full_sync_on_startup=False,  # never auto-sync; use Admin API
            )
            # Do NOT call etl_scheduler.start() — ETL is triggered manually
            # via POST /admin/etl/trigger only.
            app.state.etl_scheduler = etl_scheduler
            app.state.graphiti_client = graphiti_client
            # Inject into tool layer so graph tools can use the client
            from modules.p2p.tools import set_graphiti_client, set_query_backend
            set_graphiti_client(graphiti_client)
            # Create QueryBackend based on config (graphiti/postgresql/hybrid)
            from core.etl.query_backend import create_query_backend
            qb = create_query_backend(
                query_backend_mode=etl_cfg.query_backend,
                repository=repo,
                graphiti_client=graphiti_client,
            )
            set_query_backend(qb)
            _logger.info(
                "ETL ready: query_backend=%s (manual trigger only via Admin API)",
                etl_cfg.query_backend,
            )
    else:
        _logger.info(
            "ETL skipped: graphiti_etl.enabled=%s neo4j.enabled=%s",
            settings.graphiti_etl.enabled,
            settings.neo4j.enabled,
        )

    _logger.info("lifespan startup complete — serving traffic")
    yield
    _logger.info("lifespan shutdown: draining tasks")

    # Shutdown ETL
    if etl_scheduler is not None:
        await etl_scheduler.shutdown()
    if graphiti_client is not None:
        await graphiti_client.close()
        # TECH-DEBT(#11): shutdown 必须重置全局注入状态，防止残留已关闭的客户端引用
        from modules.p2p.tools import set_graphiti_client, set_query_backend
        set_graphiti_client(None)  # type: ignore[arg-type]
        set_query_backend(None)  # type: ignore[arg-type]
    set_graph_schema(None)
    await registry.shutdown()
    shutdown_task_registry()
    # 如果是 Redis 后端，关闭底层连接；memory 后端此调用是 no-op
    if bus is not None and hasattr(bus, "aclose"):
        try:
            await bus.aclose()
        except Exception:  # noqa: BLE001
            pass
    shutdown_event_bus()
    shutdown_trace_store()
    engine.dispose()
    _logger.info("lifespan shutdown complete")


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。

    Returns:
        配置完成的 FastAPI 应用。
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="ERP 采购到付款（P2P）流程智能分析 Agent，"
        "提供三路匹配、价格差异、付款合规、供应商绩效等多维度分析能力。",
        lifespan=lifespan,
    )

    # CORS 中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 挂载 API v1 路由
    app.include_router(analyze_router, prefix="/api/v1/ptp-agent")
    app.include_router(analyze_async_router, prefix="/api/v1/ptp-agent")
    app.include_router(sessions_router, prefix="/api/v1/ptp-agent")
    app.include_router(traces_router, prefix="/api/v1/ptp-agent")
    app.include_router(admin_metrics_router, prefix="/api/v1/ptp-agent")
    app.include_router(etl_router, prefix="/api/v1/ptp-agent")

    return app


app: FastAPI = create_app()


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, Any]:
    """健康检查端点。

    Returns:
        包含服务状态、应用名称和版本的字典。
    """
    settings = get_settings()
    return {
        "status": "ok",
        "app_name": settings.app_name,
        "version": settings.app_version,
    }


@app.post("/api/v1/ptp-agent/init-data", tags=["data"])
async def init_data() -> dict[str, Any]:
    """初始化模拟数据。

    清空所有业务表并重新灌入种子数据。
    数据条数和随机种子由 config.yaml 中 mock_data 配置决定。

    Returns:
        各表插入的记录数。
    """
    settings = get_settings()
    engine = app.state.db_engine
    counts = reset_and_seed(
        engine,
        seed=settings.mock_data.seed,
        count=settings.mock_data.record_count,
    )
    return {
        "status": "ok",
        "message": f"已重新生成 {settings.mock_data.record_count} 条模拟数据",
        "seed": settings.mock_data.seed,
        "tables": counts,
    }
