"""记忆注入效果测试。

验证长期记忆的写入 → 读取 → 注入 prompt 链路。使用真实 SQLite 记忆表。
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa

from config.settings import Settings
from core.memory.injection import format_memory_injection
from core.memory.long_term import MemoryRepository
from core.memory.manager import MemoryManager
from core.memory.tables import memories_table, metadata_obj
from core.memory.types import MemoryType
from core.time_utils import now_cn


@pytest.fixture()
def mem_engine() -> sa.engine.Engine:
    """SQLite in-memory engine with memory tables."""
    eng = sa.create_engine("sqlite:///:memory:")
    metadata_obj.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def mem_repo(mem_engine: sa.engine.Engine) -> MemoryRepository:
    return MemoryRepository(
        engine=mem_engine,
        min_content_len=0,
        dedupe_window_seconds=0,
    )


@pytest.fixture()
def settings_with_memory() -> Settings:
    """Settings with long_term_enabled=True for memory tests."""
    return Settings(
        app_name="ERP Agent Test",
        app_version="0.1.0-test",
        debug=True,
        language="zh",
    )


class TestMemoryWriteAndSearch:
    """记忆写入后可通过分类检索召回。"""

    def test_analysis_writes_entity_memory(self, mem_repo: MemoryRepository, mem_engine: sa.engine.Engine):
        """写入一条 entity_profile 记忆 → 通过 SQL 直查确认存在。"""
        mem_repo.save(
            user_id="mem-test-user",
            session_id="s1",
            memory_type=MemoryType.ENTITY_PROFILE,
            content="SUP-001 三路匹配率 92%，历史30天平均交货准时率85%",
            entity_id="SUP-001",
        )

        with mem_engine.connect() as conn:
            count = conn.execute(
                sa.select(sa.func.count()).select_from(memories_table).where(
                    memories_table.c.user_id == "mem-test-user",
                ),
            ).scalar()
        assert count >= 1

    def test_multiple_types_coexist(self, mem_repo: MemoryRepository, mem_engine: sa.engine.Engine):
        """多类型记忆写入互不覆盖。"""
        mem_repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.ENTITY_PROFILE,
            content="SUP-001 供应商画像",
            entity_id="SUP-001",
        )
        mem_repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.CORRECTION,
            content="该供应商的匹配阈值应从85%调整为90%",
            entity_id="SUP-001",
        )
        mem_repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.DOMAIN_FACT,
            content="公司三路匹配合规标准为95%以上",
        )

        with mem_engine.connect() as conn:
            count = conn.execute(
                sa.select(sa.func.count()).select_from(memories_table).where(
                    memories_table.c.user_id == "u1",
                ),
            ).scalar()
        assert count == 3


class TestMemoryInjection:
    """记忆检索结果格式化并注入 prompt。"""

    def test_memory_injected_into_context(self, mem_repo: MemoryRepository):
        """预写入 entity_profile 记忆 → search_by_type → format_memory_injection 含该信息。"""
        mem_repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.ENTITY_PROFILE,
            content="SUP-001 近30天匹配率92%，主要采购类别：办公用品",
            entity_id="SUP-001",
        )

        memories = mem_repo.search_by_type(
            user_id="u1",
            query="SUP-001 匹配",
            entity_ids=["SUP-001"],
        )
        injected = format_memory_injection(memories)
        assert "SUP-001" in injected
        assert "匹配率92%" in injected
        assert "[相关实体历史画像]" in injected

    def test_correction_memory_injected_with_priority(self, mem_repo: MemoryRepository):
        """correction 记忆在注入文本中标记为"必须遵循"。"""
        mem_repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.CORRECTION,
            content="SUP-001的三路匹配阈值应为90%而非默认85%",
            entity_id="SUP-001",
        )

        memories = mem_repo.search_by_type(
            user_id="u1",
            query="三路匹配",
            entity_ids=["SUP-001"],
            analysis_type="three_way_match",
        )
        injected = format_memory_injection(memories)
        assert "必须遵循" in injected
        assert "90%" in injected

    def test_empty_memory_returns_empty_string(self, mem_repo: MemoryRepository):
        """无记忆时注入结果为空字符串。"""
        memories = mem_repo.search_by_type(
            user_id="nonexistent-user",
            query="anything",
        )
        injected = format_memory_injection(memories)
        assert injected == ""


class TestMemoryUserIsolation:
    """不同用户的记忆严格隔离。"""

    def test_memory_user_isolation(self, mem_repo: MemoryRepository):
        """user_A 写入的记忆不出现在 user_B 的检索结果中。"""
        mem_repo.save(
            user_id="user-A", session_id="s1",
            memory_type=MemoryType.ENTITY_PROFILE,
            content="user-A 专属信息：SUP-001 有历史投诉",
            entity_id="SUP-001",
        )
        mem_repo.save(
            user_id="user-B", session_id="s2",
            memory_type=MemoryType.ENTITY_PROFILE,
            content="user-B 专属信息：SUP-002 表现优秀",
            entity_id="SUP-002",
        )

        memories_a = mem_repo.search_by_type(
            user_id="user-A", query="供应商", entity_ids=["SUP-001"],
        )
        memories_b = mem_repo.search_by_type(
            user_id="user-B", query="供应商", entity_ids=["SUP-002"],
        )

        injected_a = format_memory_injection(memories_a)
        injected_b = format_memory_injection(memories_b)

        assert "user-A 专属信息" in injected_a
        assert "user-B 专属信息" not in injected_a
        assert "user-B 专属信息" in injected_b
        assert "user-A 专属信息" not in injected_b


class TestMemoryTTLExpiry:
    """TTL 过期记忆经 purge 后不被注入。"""

    def test_memory_ttl_expiry(self, mem_repo: MemoryRepository, mem_engine: sa.engine.Engine):
        """写入已过期记忆 → purge_expired 清除 → search_by_type 不返回该记忆。"""
        from core.memory.consolidation import purge_expired

        expired_at = now_cn() - timedelta(seconds=1)

        mem_repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.ENTITY_PROFILE,
            content="已过期的供应商画像：SUP-EXPIRED 表现差",
            entity_id="SUP-EXPIRED",
            expires_at=expired_at,
        )
        mem_repo.save(
            user_id="u1", session_id="s1",
            memory_type=MemoryType.ENTITY_PROFILE,
            content="未过期的供应商画像：SUP-VALID 表现好",
            entity_id="SUP-VALID",
            expires_at=now_cn() + timedelta(days=30),
        )

        purged = purge_expired("u1", mem_engine)
        assert purged >= 1

        memories = mem_repo.search_by_type(
            user_id="u1", query="供应商", entity_ids=["SUP-EXPIRED", "SUP-VALID"],
        )
        injected = format_memory_injection(memories)
        assert "SUP-EXPIRED" not in injected
        assert "SUP-VALID" in injected
