"""记忆注入格式化测试。"""

from __future__ import annotations

from core.memory.injection import format_memory_injection


class TestFormatMemoryInjection:
    def test_empty_memories(self) -> None:
        result = format_memory_injection({})
        assert result == ""

    def test_all_empty_lists(self) -> None:
        result = format_memory_injection({
            "correction": [],
            "domain_fact": [],
            "entity_profile": [],
            "analysis_insight": [],
            "user_preference": [],
        })
        assert result == ""

    def test_correction_section(self) -> None:
        result = format_memory_injection({
            "correction": [{"content": "PO-10045 价格差异是批量折扣"}],
        })
        assert "[历史修正记录 — 分析时必须遵循]" in result
        assert "PO-10045" in result

    def test_domain_fact_section(self) -> None:
        result = format_memory_injection({
            "domain_fact": [{"content": "三路匹配容差 2%"}],
        })
        assert "[业务规则 — 分析时必须遵循]" in result

    def test_entity_profile_section(self) -> None:
        result = format_memory_injection({
            "entity_profile": [{"content": "SUP-003 匹配率 88%"}],
        })
        assert "[相关实体历史画像]" in result

    def test_multiple_sections_ordered(self) -> None:
        result = format_memory_injection({
            "correction": [{"content": "修正1"}],
            "domain_fact": [{"content": "规则1"}],
            "entity_profile": [{"content": "画像1"}],
            "user_preference": [{"content": "偏好1"}],
        })
        # correction 在 domain_fact 前面（最高优先级）
        idx_correction = result.index("[历史修正记录")
        idx_fact = result.index("[业务规则")
        idx_entity = result.index("[相关实体")
        idx_pref = result.index("[用户偏好")
        assert idx_correction < idx_fact < idx_entity < idx_pref

    def test_empty_content_skipped(self) -> None:
        result = format_memory_injection({
            "correction": [{"content": ""}, {"content": "有效内容"}],
        })
        assert "有效内容" in result
        lines = [l for l in result.split("\n") if l.startswith("- ")]
        assert len(lines) == 1

    def test_multiple_items_in_section(self) -> None:
        result = format_memory_injection({
            "entity_profile": [
                {"content": "SUP-001 匹配率 95%"},
                {"content": "SUP-003 匹配率 88%"},
            ],
        })
        assert "SUP-001" in result
        assert "SUP-003" in result
