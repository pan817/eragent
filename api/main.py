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
from api.routes.sessions import router as sessions_router
from api.routes.traces import router as traces_router
from config.settings import get_settings
from core.database import (
    P2PRepository,
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
from modules.p2p.tools import set_repository

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
    """检查 event_backend 与实际 worker 数量是否匹配；不匹配打 WARNING。

    多 worker 部署下 memory backend 会导致 POST 与 SSE 可能落在不同 worker
    进程，订阅方永远收不到发布方的事件（详见
    docs/issue/async_analyze_backend_issue.md）。此处仅打 WARNING，不阻止
    启动——运维可能临时调整 workers 数量做压测；但启动日志里能看到明显提示。
    """
    import os

    workers_env = os.environ.get("WEB_CONCURRENCY") or os.environ.get("WORKERS")
    try:
        workers = int(workers_env) if workers_env else 1
    except ValueError:
        workers = 1
    if workers > 1 and event_backend == "memory":
        _logger.warning(
            "async_analysis.event_backend=memory 但检测到 workers=%d；"
            "多 worker 下 SSE 事件无法跨进程送达，请改用 event_backend=redis。"
            "详见 docs/issue/async_analyze_backend_issue.md",
            workers,
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
    settings = get_settings()
    engine = get_engine(settings.postgresql)
    create_tables(engine)
    session_factory = get_session_factory(engine)
    app.state.settings = settings
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory
    set_repository(P2PRepository(session_factory))
    init_trace_store(session_factory)

    # 初始化会话历史 Repository（全局 + 路由模块双注入）
    chat_repo = ChatRepository(session_factory)
    init_chat_repository(chat_repo)
    from api.routes.sessions import init_chat_repo
    init_chat_repo(chat_repo)

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
    )
    # 跨进程残留收敛：把上次进程中残留的 running / pending 标记为 aborted / error
    registry.recover_on_startup()
    registry.start_background()
    yield
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
