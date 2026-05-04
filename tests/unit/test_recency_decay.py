"""时间衰减权重计算 (core/memory/recency.py) 单元测试。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from core.memory.recency import apply_recency_decay
from core.time_utils import now_cn


class TestApplyRecencyDecay:
    """apply_recency_decay 核心算法。"""

    def test_empty_list(self) -> None:
        result = apply_recency_decay([])
        assert result == []

    def test_today_item_score_near_one(self) -> None:
        items = [{"created_at": now_cn(), "base_score": 1.0}]
        apply_recency_decay(items, lambda_val=0.02)
        assert items[0]["recency_score"] == pytest.approx(1.0, abs=0.01)

    def test_30_days_ago_score_about_55_pct(self) -> None:
        items = [{"created_at": now_cn() - timedelta(days=30), "base_score": 1.0}]
        apply_recency_decay(items, lambda_val=0.02)
        expected = math.exp(-0.02 * 30)
        assert items[0]["recency_score"] == pytest.approx(expected, rel=0.01)

    def test_365_days_ago(self) -> None:
        items = [{"created_at": now_cn() - timedelta(days=365), "base_score": 1.0}]
        apply_recency_decay(items, lambda_val=0.02)
        expected = math.exp(-0.02 * 365)
        assert items[0]["recency_score"] == pytest.approx(expected, rel=0.01)

    def test_sorts_by_recency_score_desc(self) -> None:
        now = now_cn()
        items = [
            {"created_at": now - timedelta(days=60), "base_score": 1.0, "id": "old"},
            {"created_at": now - timedelta(days=1), "base_score": 1.0, "id": "new"},
            {"created_at": now - timedelta(days=30), "base_score": 1.0, "id": "mid"},
        ]
        apply_recency_decay(items, lambda_val=0.02)
        ids = [i["id"] for i in items]
        assert ids == ["new", "mid", "old"]

    def test_base_score_multiplied(self) -> None:
        now = now_cn()
        items = [{"created_at": now - timedelta(days=10), "base_score": 2.0}]
        apply_recency_decay(items, lambda_val=0.02)
        expected = 2.0 * math.exp(-0.02 * 10)
        assert items[0]["recency_score"] == pytest.approx(expected, rel=0.01)

    def test_default_base_score_is_one(self) -> None:
        items = [{"created_at": now_cn()}]
        apply_recency_decay(items, lambda_val=0.02)
        assert items[0]["recency_score"] == pytest.approx(1.0, abs=0.01)


class TestDecayDisabled:
    """enabled=False 时不施加衰减。"""

    def test_disabled_keeps_base_score(self) -> None:
        items = [
            {"created_at": now_cn() - timedelta(days=100), "base_score": 0.8},
        ]
        apply_recency_decay(items, lambda_val=0.02, enabled=False)
        assert items[0]["recency_score"] == 0.8

    def test_disabled_still_adds_field(self) -> None:
        items = [{"created_at": now_cn()}]
        apply_recency_decay(items, enabled=False)
        assert "recency_score" in items[0]


class TestEdgeCases:
    """边界条件。"""

    def test_missing_created_at_gets_base_score(self) -> None:
        items = [{"base_score": 0.5}]
        apply_recency_decay(items, lambda_val=0.02)
        assert items[0]["recency_score"] == 0.5

    def test_iso_string_created_at(self) -> None:
        ts = (now_cn() - timedelta(days=10)).isoformat()
        items = [{"created_at": ts, "base_score": 1.0}]
        apply_recency_decay(items, lambda_val=0.02)
        expected = math.exp(-0.02 * 10)
        assert items[0]["recency_score"] == pytest.approx(expected, abs=0.05)

    def test_invalid_string_created_at(self) -> None:
        items = [{"created_at": "not-a-date", "base_score": 0.9}]
        apply_recency_decay(items, lambda_val=0.02)
        assert items[0]["recency_score"] == 0.9

    def test_future_date_no_negative_decay(self) -> None:
        items = [{"created_at": now_cn() + timedelta(days=5), "base_score": 1.0}]
        apply_recency_decay(items, lambda_val=0.02)
        assert items[0]["recency_score"] == pytest.approx(1.0, abs=0.01)

    def test_zero_lambda(self) -> None:
        items = [{"created_at": now_cn() - timedelta(days=100), "base_score": 1.0}]
        apply_recency_decay(items, lambda_val=0.0)
        assert items[0]["recency_score"] == pytest.approx(1.0, abs=0.001)
