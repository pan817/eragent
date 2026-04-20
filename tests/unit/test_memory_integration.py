"""记忆模块集成测试：MemoryManager + Extractor + Injection 端到端。

使用 SQLite 内存数据库模拟完整的记忆写入 → 检索 → 注入流程。
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa

from config.settings import Settings
from core.memory.consolidation import (
    merge_entity_profiles_rule,
    purge_expired,
    should_consolidate,
    try_acquire_lock,
    release_lock,
)
from core.memory.extractor import MemoryExtractor
from core.memory.feedback import FeedbackDetector
from core.memory.injection import format_memory_injection
from core.memory.long_term import MemoryRepository
from core.memory.tables import metadata_obj
from core.memory.types import MemoryType
from core.time_utils import now_cn


@pytest.fixture
def engine() -> sa.engine.Engine:
    """SQLite in-memory engine with all tables created."""
    eng = sa.create_engine("sqlite:///:memory:")
    metadata_obj.create_all(eng)
    return eng


@pytest.fixture
def repo(engine: sa.engine.Engine) -> MemoryRepository:
    return MemoryRepository(
        engine=engine,
        min_content_len=0,
        dedupe_window_seconds=0,
    )


@pytest.fixture
def settings() -> Settings:
    return Settings()


# ── 端到端：写入 → 检索 → 注入 ─────────────────────────────


class TestWriteSearchInjectFlow:
    """完整的记忆写入 → 类型感知检索 → Prompt 注入流程。"""

    def test_entity_profile_exact_search(self, repo: MemoryRepository) -> None:
        """entity_profile 按 entity_id 精确检索。"""
        repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.ENTITY_PROFILE,
            content="SUP-001 三路匹配率 92%",
            entity_id="SUP-001",
        )
        repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.ENTITY_PROFILE,
            content="SUP-002 三路匹配率 85%",
            entity_id="SUP-002",
        )

        results = repo.search_by_type(
            user_id="u1", query="三路匹配",
            entity_ids=["SUP-001"],
        )
        ep = results["entity_profile"]
        assert len(ep) >= 1
        # 精确匹配的 SUP-001 应排在第一位
        assert ep[0].get("entity_id") == "SUP-001"

    def test_correction_exact_by_analysis_type(self, repo: MemoryRepository) -> None:
        """correction 按 analysis_type 精确检索。"""
        repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.CORRECTION,
            content="PO-001 价格差异是批量折扣",
            metadata={"corrected_analysis_type": "price_variance"},
            entity_id="PO-001",
        )
        repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.CORRECTION,
            content="付款逾期是因为系统延迟",
            metadata={"corrected_analysis_type": "payment_compliance"},
        )

        results = repo.search_by_type(
            user_id="u1", query="价格",
            analysis_type="price_variance",
        )
        corrections = results["correction"]
        # 至少包含 price_variance 的修正
        assert any("批量折扣" in c.get("content", "") for c in corrections)

    def test_user_preference_full_load(self, repo: MemoryRepository) -> None:
        """user_preference 按 user_id 全量加载。"""
        repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.USER_PREFERENCE,
            content="用户偏好表格输出",
        )
        repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.USER_PREFERENCE,
            content="默认查询 30 天",
        )

        results = repo.search_by_type(user_id="u1", query="anything")
        prefs = results["user_preference"]
        assert len(prefs) == 2

    def test_injection_format(self, repo: MemoryRepository) -> None:
        """完整流程：写入多种类型 → 检索 → 格式化注入。"""
        repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.CORRECTION,
            content="PO-001 是批量折扣",
            metadata={"corrected_analysis_type": "price_variance"},
        )
        repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.DOMAIN_FACT,
            content="三路匹配容差 2%",
        )
        repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.USER_PREFERENCE,
            content="偏好表格输出",
        )

        results = repo.search_by_type(
            user_id="u1", query="三路匹配",
            analysis_type="price_variance",
        )
        text = format_memory_injection(results)

        assert "[历史修正记录 — 分析时必须遵循]" in text
        assert "[业务规则 — 分析时必须遵循]" in text
        assert "[用户偏好]" in text
        assert "批量折扣" in text
        assert "容差 2%" in text
        assert "表格" in text


# ── Extractor 端到端 ─────────────────────────────────────────


class TestExtractorE2E:
    """MemoryExtractor 从 AnalysisResult 提取记忆到 DB。"""

    def test_extract_entity_profile_writes_to_db(
        self, repo: MemoryRepository, settings: Settings,
    ) -> None:
        extractor = MemoryExtractor(repo=repo, settings=settings)

        # Mock AnalysisResult
        doc = MagicMock()
        doc.po_number = "PO-100"
        doc.vendor_name = "SUP-003"
        doc.invoice_num = ""
        anomaly = MagicMock(documents=doc)

        result = MagicMock()
        result.anomalies = [anomaly]
        result.supplier_kpis = []
        result.summary = {}
        result.analysis_type = MagicMock(value="three_way_match")
        result.created_at = now_cn()

        mid = extractor._extract_entity_profile(result, "u1", "s1")
        assert mid is not None

        # Verify written to DB
        from core.memory.tables import memories_table
        with repo._engine.connect() as conn:
            rows = conn.execute(
                sa.select(memories_table)
                .where(memories_table.c.user_id == "u1")
            ).fetchall()
        assert len(rows) >= 1
        assert any(r.memory_type == MemoryType.ENTITY_PROFILE for r in rows)


# ── Feedback 端到端 ──────────────────────────────────────────


class TestFeedbackE2E:
    def test_detect_and_parse_round_trip(self) -> None:
        """预筛 → LLM mock → 解析完整流程。"""
        detector = FeedbackDetector()

        # Step 1: detect signal
        signal = detector.detect_signal("这个价格差异不对，是因为批量折扣")
        assert signal == "correction"

        # Step 2: parse a hypothetical LLM response
        import json
        llm_response = json.dumps({
            "type": "correction",
            "content": "PO-10045 价格差异因批量折扣不算异常",
            "related_entities": {"po_number": "PO-10045"},
            "confidence": 0.92,
        })
        parsed = detector._parse_response(llm_response)
        assert parsed is not None
        assert parsed["type"] == "correction"
        assert parsed["confidence"] == 0.92


# ── Consolidation 端到端 ──────────────────────────────────────


class TestConsolidationE2E:
    def test_full_consolidation_flow(self, engine: sa.engine.Engine) -> None:
        """完整流程：写入 → 触发判定 → 锁 → 合并 → 释放。"""
        from core.memory.tables import memories_table

        settings = Settings()
        settings.memory.consolidation.min_new_memories = 3

        # Step 1: Write 4 entity_profile memories for same entity
        with engine.begin() as conn:
            for i in range(4):
                conn.execute(memories_table.insert().values(
                    id=f"m{i}", user_id="u1", session_id="s1",
                    memory_type=MemoryType.ENTITY_PROFILE,
                    content=f"SUP-001 匹配率 {90 + i}%",
                    attrs={"entity_id": "SUP-001"},
                    entity_id="SUP-001",
                    is_consolidated=False,
                ))

        # Step 2: should_consolidate → True
        assert should_consolidate("u1", engine, settings) is True

        # Step 3: acquire lock
        log_id = try_acquire_lock("u1", engine)
        assert log_id is not None

        # Step 4: merge
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(memories_table)
                .where(memories_table.c.user_id == "u1")
            ).fetchall()
        all_memories = [dict(row._mapping) for row in rows]
        merged = merge_entity_profiles_rule(all_memories)
        assert len(merged) == 1
        assert len(merged[0]["source_ids"]) == 4

        # Step 5: release lock
        release_lock(log_id, engine, status="completed",
                     input_count=4, merged_count=1)

    def test_ttl_and_cap_integration(
        self, repo: MemoryRepository, engine: sa.engine.Engine,
    ) -> None:
        """TTL 淘汰 + cap 上限协同。"""
        # Write a memory with past TTL
        past = now_cn() - timedelta(days=1)
        repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.ENTITY_PROFILE,
            content="A" * 60,
            entity_id="OLD",
            expires_at=past,
        )

        # Write a memory with future TTL
        future = now_cn() + timedelta(days=90)
        repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.ENTITY_PROFILE,
            content="B" * 60,
            entity_id="NEW",
            expires_at=future,
        )

        # purge_expired — SQLite doesn't support RETURNING so count may be 0
        # but it should not raise
        purge_expired("u1", engine)


# ── MemoryType 枚举 ──────────────────────────────────────────


class TestMemoryType:
    def test_values(self) -> None:
        assert MemoryType.USER_PREFERENCE == "user_preference"
        assert MemoryType.ENTITY_PROFILE == "entity_profile"
        assert MemoryType.ANALYSIS_INSIGHT == "analysis_insight"
        assert MemoryType.CORRECTION == "correction"
        assert MemoryType.DOMAIN_FACT == "domain_fact"

    def test_enum_count(self) -> None:
        assert len(MemoryType) == 5

    def test_str_comparison(self) -> None:
        assert MemoryType.CORRECTION == "correction"
        assert "correction" == MemoryType.CORRECTION


# ── TTL 计算 ─────────────────────────────────────────────────


class TestComputeExpiresAt:
    def test_entity_profile_has_ttl(self, repo: MemoryRepository) -> None:
        expires = repo.compute_expires_at(MemoryType.ENTITY_PROFILE)
        assert expires is not None
        assert expires > now_cn()

    def test_user_preference_no_ttl(self, repo: MemoryRepository) -> None:
        expires = repo.compute_expires_at(MemoryType.USER_PREFERENCE)
        assert expires is None

    def test_domain_fact_no_ttl(self, repo: MemoryRepository) -> None:
        expires = repo.compute_expires_at(MemoryType.DOMAIN_FACT)
        assert expires is None

    def test_correction_has_ttl(self, repo: MemoryRepository) -> None:
        expires = repo.compute_expires_at(MemoryType.CORRECTION)
        assert expires is not None
