"""多轮对话上下文继承测试。

验证短期记忆的实体持久化、会话隔离、bypass 路由在连续查询中的表现。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from config.settings import Settings
from core.database import install_sqlite_timezone_hook
from core.memory.short_term import ShortTermMemory
from core.memory.tables import metadata_obj
from core.orchestrator.router import _classify_bypass, _extract_params
from core.orchestrator.signal import IntentKind


@pytest.fixture()
def short_term(settings: Settings) -> ShortTermMemory:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    install_sqlite_timezone_hook(engine)
    metadata_obj.create_all(engine)
    mem = ShortTermMemory(settings=settings)
    mem._entity_engine = engine
    yield mem
    engine.dispose()


class TestEntityPersistence:
    """实体上下文在同一 session 中持久化。"""

    def test_save_and_load_entities(self, short_term):
        short_term.save_entity_context(
            "sess-1", {"vendor_id": "SUP-001", "po_number": "PO-001"},
        )
        ctx = short_term.load_session_context("sess-1")
        assert ctx["entities"].get("vendor_id") == "SUP-001"
        assert ctx["entities"].get("po_number") == "PO-001"

    def test_entities_update_across_turns(self, short_term):
        short_term.save_entity_context("sess-2", {"vendor_id": "SUP-001"})
        short_term.save_entity_context("sess-2", {"vendor_id": "SUP-002"})
        ctx = short_term.load_session_context("sess-2")
        assert ctx["entities"].get("vendor_id") == "SUP-002"


class TestSessionIsolation:
    """不同 session 的实体互不泄漏。"""

    def test_sessions_isolated(self, short_term):
        short_term.save_entity_context("sess-A", {"vendor_id": "SUP-001"})
        short_term.save_entity_context("sess-B", {"vendor_id": "SUP-002"})

        ctx_a = short_term.load_session_context("sess-A")
        ctx_b = short_term.load_session_context("sess-B")

        assert ctx_a["entities"].get("vendor_id") == "SUP-001"
        assert ctx_b["entities"].get("vendor_id") == "SUP-002"

    def test_empty_session_returns_empty(self, short_term):
        ctx = short_term.load_session_context("sess-nonexistent")
        assert isinstance(ctx, dict)


class TestParamExtractionAcrossTurns:
    """参数提取在连续查询中的一致性。"""

    def test_extract_then_bypass(self):
        p1 = _extract_params("查看SUP-001的订单")
        assert p1.get("vendor_id") == "SUP-001"

        bypass = _classify_bypass("那个供应商的发票呢")
        assert bypass is None

    def test_recall_after_entity_query(self):
        p1 = _extract_params("分析PO-2024-0001的匹配情况")
        assert p1.get("po_number") == "PO-2024-0001"

        bypass = _classify_bypass("上次分析了什么")
        assert bypass == IntentKind.RECALL
