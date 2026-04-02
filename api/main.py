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
from config.settings import get_settings
from core.database import (
    P2PRepository,
    create_tables,
    get_engine,
    get_session_factory,
    reset_and_seed,
)
from modules.p2p.tools import set_repository


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
    yield
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
    app.include_router(analyze_router, prefix="/api/v1")

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


@app.post("/api/v1/init-data", tags=["data"])
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
