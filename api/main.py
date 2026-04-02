"""
FastAPI 应用入口。

创建并配置 FastAPI 应用实例，挂载路由、中间件，
提供健康检查端点。Agent 等重量级组件采用延迟加载策略。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.analyze import router as analyze_router
from config.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理。

    启动时初始化全局配置（轻量级），
    Agent 等重量级组件由 Orchestrator 延迟加载，不在此处初始化。

    Args:
        app: FastAPI 应用实例。

    Yields:
        None
    """
    settings = get_settings()
    app.state.settings = settings
    yield


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
