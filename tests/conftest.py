"""
共享测试 fixtures。

通用基础设施 fixture（Settings、DB）在此文件。
P2P 模块特有 fixture 在 tests/fixtures/p2p.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import Settings  # noqa: E402
from core.database import (  # noqa: E402
    Base,
    get_session_factory,
    init_database,
    install_sqlite_timezone_hook,
)
from core.database.engine import create_engine_from_dsn  # noqa: E402
from core.time_utils import configure_timezone  # noqa: E402

# 加载 P2P 模块 fixtures
pytest_plugins = ["tests.fixtures.p2p"]

# 统一测试时区
configure_timezone("Asia/Shanghai")


@pytest.fixture(autouse=True)
def _enforce_test_timezone():
    """每个测试强制业务时区为 Asia/Shanghai。"""
    configure_timezone("Asia/Shanghai")
    yield


# ============================================================
# 配置 fixtures
# ============================================================


@pytest.fixture()
def settings() -> Settings:
    """创建测试用 Settings 实例（不依赖 config.yaml）。"""
    return Settings(
        app_name="ERP Agent Test",
        app_version="0.1.0-test",
        debug=True,
        language="zh",
    )


# ============================================================
# 数据库 fixtures（SQLite 内存库）
# ============================================================


@pytest.fixture()
def db_engine():
    """创建 SQLite 内存数据库引擎，灌入种子数据。"""
    from sqlalchemy.pool import StaticPool

    engine = create_engine_from_dsn(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    install_sqlite_timezone_hook(engine)
    init_database(engine, seed=0)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session_factory(db_engine):
    """返回绑定到测试引擎的 Session 工厂。"""
    return get_session_factory(db_engine)
