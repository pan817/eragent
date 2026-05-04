"""真实工具链 fixture（repo + tool 注入 + registry）。

functional 层测试直接获得"注入好 repo 的真实工具 + ToolRegistry"。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.pool import StaticPool

from config.settings import Settings
from core.database import (
    get_session_factory,
    init_database,
    install_sqlite_timezone_hook,
)
from core.database.engine import create_engine_from_dsn
from core.orchestrator.dag.registry import ToolRegistry, build_registry_from_provider
from modules.p2p.mock_data.generator import MockDataGenerator
from modules.p2p.provider import P2PModuleProvider
from modules.p2p.repository import P2PRepository
from modules.p2p.tools import set_repository


@pytest.fixture(scope="session")
def seeded_engine():
    """会话级 SQLite 内存引擎，灌入 50 条种子数据。"""
    engine = create_engine_from_dsn(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    install_sqlite_timezone_hook(engine)
    init_database(engine, seed=0, count=50, data_generator_factory=MockDataGenerator)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def seeded_repo(seeded_engine) -> P2PRepository:
    """会话级真实 Repository（绑定种子数据引擎）。"""
    return P2PRepository(get_session_factory(seeded_engine))


@pytest.fixture()
def inject_seeded_repo(seeded_repo: P2PRepository):
    """注入 seeded_repo 到工具模块全局变量。

    functional/regression 层 conftest 中 autouse 引用此 fixture。
    """
    set_repository(seeded_repo)
    yield
    set_repository(None)


@pytest.fixture(scope="session")
def tool_registry(seeded_repo: P2PRepository) -> ToolRegistry:
    """会话级 ToolRegistry，注册全部 PG 工具。"""
    set_repository(seeded_repo)
    provider = P2PModuleProvider()
    return build_registry_from_provider(provider)


@pytest.fixture()
def seed_data_summary() -> dict[str, Any]:
    """返回种子数据的统计摘要，供断言参考。"""
    gen = MockDataGenerator(seed=0)
    data = gen.generate_all(count=50)
    headers = data["po_headers"]
    by_date = sorted(headers, key=lambda h: h["creation_date"])
    by_amount = sorted(headers, key=lambda h: h["total_amount"], reverse=True)
    return {
        "po_count": len(headers),
        "supplier_ids": sorted({h["vendor_id"] for h in headers}),
        "first_po": by_date[0]["po_number"],
        "latest_po": by_date[-1]["po_number"],
        "max_amount_po": by_amount[0]["po_number"],
    }


@pytest.fixture()
def p2p_provider() -> P2PModuleProvider:
    """P2PModuleProvider 实例。"""
    return P2PModuleProvider()
