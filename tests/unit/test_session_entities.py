"""多轮跨实体查询 — session_entities 测试。

覆盖：
- T1: entity_context 基础 CRUD（_load / save / merge）
- T2: 指代消解 + entity_context 联动（5 种实体）
- T3: 多轮跨实体场景（级联、上下文切换、GR 前缀）
- T4: orchestrator._save_session_entities（parsed_params + report 提取）
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from config.settings import Settings
from core.database import install_sqlite_timezone_hook
from core.memory.short_term import ShortTermMemory
from core.memory.tables import metadata_obj, session_entities_table


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def entity_engine():
    """SQLite 内存引擎，仅建 session_entities 表。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    install_sqlite_timezone_hook(engine)
    metadata_obj.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def stm(entity_engine, settings):
    """ShortTermMemory 实例，entity_context 读写指向测试引擎。"""
    mem = ShortTermMemory(settings=settings)
    mem._entity_engine = entity_engine
    return mem


# ============================================================
# T1: entity_context 基础 CRUD
# ============================================================


class TestEntityContextCRUD:
    """基础读写测试。"""

    def test_load_empty(self, stm):
        """空 session 读取返回空 dict。"""
        result = stm._load_entity_context("non-existent-session")
        assert result == {}

    def test_save_and_load(self, stm):
        """写入后能读回。"""
        entities = {"po_number": "PO-001", "supplier_id": "SUP-001"}
        stm.save_entity_context("sess-1", entities)

        loaded = stm._load_entity_context("sess-1")
        assert loaded == entities

    def test_save_merge_preserves_old_keys(self, stm):
        """第二次写入 merge：新 key 追加，旧 key 保留。"""
        stm.save_entity_context("sess-2", {"po_number": "PO-001"})
        stm.save_entity_context("sess-2", {"supplier_id": "SUP-001"})

        loaded = stm._load_entity_context("sess-2")
        assert loaded == {"po_number": "PO-001", "supplier_id": "SUP-001"}

    def test_save_merge_overwrites_same_key(self, stm):
        """同名 key 新值覆盖旧值。"""
        stm.save_entity_context("sess-3", {"po_number": "PO-001"})
        stm.save_entity_context("sess-3", {"po_number": "PO-002"})

        loaded = stm._load_entity_context("sess-3")
        assert loaded["po_number"] == "PO-002"

    def test_save_filters_non_entity_keys(self, stm):
        """days 等非实体 key 不写入。"""
        stm.save_entity_context("sess-4", {
            "po_number": "PO-001",
            "days": 30,
            "random_key": "ignored",
        })

        loaded = stm._load_entity_context("sess-4")
        assert loaded == {"po_number": "PO-001"}
        assert "days" not in loaded
        assert "random_key" not in loaded

    def test_save_skips_empty_values(self, stm):
        """空值实体不写入。"""
        stm.save_entity_context("sess-5", {
            "po_number": "PO-001",
            "supplier_id": "",
            "invoice_number": None,
        })

        loaded = stm._load_entity_context("sess-5")
        assert loaded == {"po_number": "PO-001"}

    def test_different_sessions_isolated(self, stm):
        """不同 session 的实体互不干扰。"""
        stm.save_entity_context("sess-a", {"po_number": "PO-001"})
        stm.save_entity_context("sess-b", {"po_number": "PO-999"})

        assert stm._load_entity_context("sess-a")["po_number"] == "PO-001"
        assert stm._load_entity_context("sess-b")["po_number"] == "PO-999"


# ============================================================
# T2: 指代消解 + entity_context 联动
# ============================================================


class TestReferenceResolutionWithEntityContext:
    """指代消解使用 entity_context 中的实体。"""

    @pytest.mark.parametrize("query,entity_key,entity_val,expected_fragment", [
        # 5 种实体的指代消解
        ("这个PO的供应商绩效", "po_number", "PO-2024-0001", "采购订单 PO-2024-0001"),
        ("该供应商的交期表现", "supplier_id", "SUP-001", "供应商 SUP-001"),
        ("这个付款单的状态", "payment_number", "PAY-001", "付款单 PAY-001"),
        ("该发票的三路匹配", "invoice_number", "INV-001", "发票 INV-001"),
        ("这个收货单对应的PO", "receipt_number", "RCV-001", "收货单 RCV-001"),
    ])
    def test_reference_resolved_from_entity_context(
        self, stm, query, entity_key, entity_val, expected_fragment,
    ):
        """entity_context 中的实体能被指代词正确消解。"""
        from core.orchestrator.entity import resolve_references

        # 模拟 load_session_context 的返回结构
        session_ctx = {
            "has_history": True,
            "context_summary": "",
            "entities": {entity_key: entity_val},
        }

        enhanced_query, relevant = resolve_references(query, session_ctx)

        assert expected_fragment in enhanced_query
        assert relevant.get(entity_key) == entity_val

    def test_no_reference_no_inheritance(self):
        """无指代词时不继承历史实体。"""
        from core.orchestrator.entity import resolve_references

        session_ctx = {
            "has_history": True,
            "context_summary": "",
            "entities": {"po_number": "PO-001"},
        }

        enhanced, relevant = resolve_references("分析价格差异", session_ctx)

        assert enhanced == "分析价格差异"  # 不变
        assert relevant == {}  # 不继承

    def test_generic_reference_inherits_all(self):
        """通用指代（"它的"）继承所有历史实体。"""
        from core.orchestrator.entity import resolve_references

        session_ctx = {
            "has_history": True,
            "context_summary": "",
            "entities": {"po_number": "PO-001", "supplier_id": "SUP-001"},
        }

        _, relevant = resolve_references("它的情况怎么样", session_ctx)

        assert relevant == {"po_number": "PO-001", "supplier_id": "SUP-001"}


# ============================================================
# T3: 多轮跨实体场景
# ============================================================


class TestMultiTurnCrossEntityScenarios:
    """模拟多轮对话的实体传递与级联。"""

    def test_po_to_supplier_cascade(self, stm):
        """轮1写入PO → 轮2通过PO关联supplier → 轮3两者都在。"""
        # 轮 1：写入 PO
        stm.save_entity_context("multi-1", {"po_number": "PO-001"})

        # 轮 2：PO 仍在，追加 supplier（模拟 enrich_entities 结果）
        stm.save_entity_context("multi-1", {
            "po_number": "PO-001",
            "supplier_id": "SUP-001",
        })

        # 轮 3：读取——两个实体都在
        entities = stm._load_entity_context("multi-1")
        assert entities["po_number"] == "PO-001"
        assert entities["supplier_id"] == "SUP-001"

    def test_invoice_to_po_to_supplier_three_level(self, stm):
        """发票 → PO → 供应商三级级联。"""
        # 轮 1：只有发票
        stm.save_entity_context("multi-2", {"invoice_number": "INV-001"})

        # 轮 2：enrich 补充了 PO 和 supplier
        stm.save_entity_context("multi-2", {
            "invoice_number": "INV-001",
            "po_number": "PO-001",
            "supplier_id": "SUP-001",
        })

        entities = stm._load_entity_context("multi-2")
        assert entities == {
            "invoice_number": "INV-001",
            "po_number": "PO-001",
            "supplier_id": "SUP-001",
        }

    def test_context_switch_overwrites_stale_entity(self, stm):
        """用户切换 PO 号时，旧 supplier 被新 enrich 覆盖。"""
        # 轮 1-2：PO-001 + SUP-001
        stm.save_entity_context("multi-3", {
            "po_number": "PO-001",
            "supplier_id": "SUP-001",
        })

        # 轮 3：用户切换到 PO-002，enrich 得到 SUP-002
        stm.save_entity_context("multi-3", {
            "po_number": "PO-002",
            "supplier_id": "SUP-002",
        })

        entities = stm._load_entity_context("multi-3")
        assert entities["po_number"] == "PO-002"
        assert entities["supplier_id"] == "SUP-002"  # 旧 SUP-001 被覆盖

    def test_gr_prefix_receipt_number(self, stm):
        """GR-前缀收货单号正确存储和读取。"""
        stm.save_entity_context("multi-4", {"receipt_number": "GR-0001"})

        entities = stm._load_entity_context("multi-4")
        assert entities["receipt_number"] == "GR-0001"

    def test_payment_to_invoice_cascade(self, stm):
        """付款单 → 发票级联。"""
        stm.save_entity_context("multi-5", {"payment_number": "PAY-001"})
        stm.save_entity_context("multi-5", {
            "payment_number": "PAY-001",
            "invoice_number": "INV-001",
        })

        entities = stm._load_entity_context("multi-5")
        assert entities["payment_number"] == "PAY-001"
        assert entities["invoice_number"] == "INV-001"

    def test_all_five_entities_coexist(self, stm):
        """5 种实体全部共存。"""
        all_entities = {
            "po_number": "PO-001",
            "supplier_id": "SUP-001",
            "invoice_number": "INV-001",
            "payment_number": "PAY-001",
            "receipt_number": "RCV-001",
        }
        stm.save_entity_context("multi-6", all_entities)

        loaded = stm._load_entity_context("multi-6")
        assert loaded == all_entities

    def test_partial_update_preserves_unmentioned_entities(self, stm):
        """轮2只提到supplier，轮1的PO不丢失。"""
        stm.save_entity_context("multi-7", {"po_number": "PO-001"})
        # 轮 2 只有 supplier（指代消解走了"该供应商"路径）
        stm.save_entity_context("multi-7", {"supplier_id": "SUP-001"})

        entities = stm._load_entity_context("multi-7")
        # 两者都在
        assert entities["po_number"] == "PO-001"
        assert entities["supplier_id"] == "SUP-001"


# ============================================================
# T4: orchestrator._save_session_entities
# ============================================================


class TestOrchestratorSaveSessionEntities:
    """测试从 parsed_params + report_markdown 合并写入实体。"""

    @pytest.fixture()
    def orchestrator(self, stm, settings):
        """构造最小 Orchestrator 实例（仅测 _save_session_entities）。"""
        from unittest.mock import MagicMock

        from core.orchestrator.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._settings = settings
        orch._short_term = stm
        return orch

    @pytest.mark.asyncio
    async def test_save_from_parsed_params(self, orchestrator, stm):
        """parsed_params 中的实体写入 session_entities。"""
        from api.schemas.domain import AnalysisResult, AnalysisStatus, AnalysisType

        result = AnalysisResult(
            report_id="r1",
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.THREE_WAY_MATCH,
            query="分析PO-001",
            user_id="u1",
            session_id="orch-1",
            time_range="30天",
        )
        parsed_params = {
            "po_number": "PO-001",
            "supplier_id": "SUP-001",
            "days": 30,
        }

        await orchestrator._save_session_entities("orch-1", parsed_params, result)

        entities = stm._load_entity_context("orch-1")
        assert entities["po_number"] == "PO-001"
        assert entities["supplier_id"] == "SUP-001"
        assert "days" not in entities

    @pytest.mark.asyncio
    async def test_save_extracts_from_report_markdown(self, orchestrator, stm):
        """report_markdown 中新出现的实体编号被补充提取。"""
        from api.schemas.domain import AnalysisResult, AnalysisStatus, AnalysisType

        result = AnalysisResult(
            report_id="r2",
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query="查最新PO",
            user_id="u1",
            session_id="orch-2",
            time_range="30天",
            report_markdown="最新的采购订单是 PO-2024-0001，供应商 SUP-003",
        )
        # parsed_params 中没有 PO 编号（query 中无编号）
        parsed_params = {"days": 30}

        await orchestrator._save_session_entities("orch-2", parsed_params, result)

        entities = stm._load_entity_context("orch-2")
        assert entities["po_number"] == "PO-2024-0001"
        assert entities["supplier_id"] == "SUP-003"

    @pytest.mark.asyncio
    async def test_parsed_params_takes_priority_over_report(self, orchestrator, stm):
        """parsed_params 中的实体优先于 report 中提取的。"""
        from api.schemas.domain import AnalysisResult, AnalysisStatus, AnalysisType

        result = AnalysisResult(
            report_id="r3",
            status=AnalysisStatus.SUCCESS,
            analysis_type=AnalysisType.SUPPLIER_PERFORMANCE,
            query="SUP-001的绩效",
            user_id="u1",
            session_id="orch-3",
            time_range="30天",
            report_markdown="对比供应商 SUP-001 和 SUP-002 的绩效",
        )
        parsed_params = {"supplier_id": "SUP-001", "days": 30}

        await orchestrator._save_session_entities("orch-3", parsed_params, result)

        entities = stm._load_entity_context("orch-3")
        # parsed_params 的 SUP-001 优先，report 的 SUP-002 不覆盖
        assert entities["supplier_id"] == "SUP-001"
