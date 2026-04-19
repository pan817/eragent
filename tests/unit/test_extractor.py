"""MemoryExtractor 单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config.settings import Settings
from core.memory.extractor import MemoryExtractor
from core.memory.types import MemoryType


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def mock_repo() -> MagicMock:
    repo = MagicMock()
    repo.save.return_value = "mem_001"
    repo.search.return_value = []
    repo.compute_expires_at.return_value = None
    return repo


@pytest.fixture
def extractor(mock_repo: MagicMock, settings: Settings) -> MemoryExtractor:
    return MemoryExtractor(repo=mock_repo, settings=settings)


class TestCollectEntities:
    def test_empty_result(self, extractor: MemoryExtractor) -> None:
        result = MagicMock()
        result.anomalies = []
        result.supplier_kpis = []
        result.summary = {}
        assert extractor._collect_entities(result) == []

    def test_extracts_from_anomalies(self, extractor: MemoryExtractor) -> None:
        doc = MagicMock()
        doc.po_number = "PO-001"
        doc.supplier_name = "SUP-003"
        doc.invoice_number = ""
        anomaly = MagicMock()
        anomaly.documents = doc

        result = MagicMock()
        result.anomalies = [anomaly]
        result.supplier_kpis = []
        result.summary = {}

        entities = extractor._collect_entities(result)
        assert ("po", "PO-001") in entities
        assert ("supplier", "SUP-003") in entities

    def test_dedup(self, extractor: MemoryExtractor) -> None:
        doc = MagicMock()
        doc.po_number = "PO-001"
        doc.supplier_name = ""
        doc.invoice_number = ""
        a1 = MagicMock(documents=doc)
        a2 = MagicMock(documents=doc)

        result = MagicMock()
        result.anomalies = [a1, a2]
        result.supplier_kpis = []
        result.summary = {}

        entities = extractor._collect_entities(result)
        assert entities.count(("po", "PO-001")) == 1


class TestExtractEntityProfile:
    def test_no_entities_returns_none(
        self, extractor: MemoryExtractor, mock_repo: MagicMock,
    ) -> None:
        result = MagicMock()
        result.anomalies = []
        result.supplier_kpis = []
        result.summary = {}
        result.analysis_type = MagicMock(value="three_way_match")
        result.created_at = None

        mid = extractor._extract_entity_profile(result, "u1", "s1")
        assert mid is None
        mock_repo.save.assert_not_called()

    def test_saves_entity_profile(
        self, extractor: MemoryExtractor, mock_repo: MagicMock,
    ) -> None:
        doc = MagicMock()
        doc.po_number = "PO-001"
        doc.supplier_name = ""
        doc.invoice_number = ""
        anomaly = MagicMock(documents=doc)

        result = MagicMock()
        result.anomalies = [anomaly]
        result.supplier_kpis = []
        result.summary = {}
        result.analysis_type = MagicMock(value="three_way_match")
        result.created_at = None

        mid = extractor._extract_entity_profile(result, "u1", "s1")
        assert mid == "mem_001"
        mock_repo.save.assert_called_once()
        call_kwargs = mock_repo.save.call_args
        assert call_kwargs.kwargs.get("memory_type") == MemoryType.ENTITY_PROFILE or \
               call_kwargs[1].get("memory_type") == MemoryType.ENTITY_PROFILE


class TestExtractAnalysisInsight:
    def test_insufficient_history(
        self, extractor: MemoryExtractor, mock_repo: MagicMock,
    ) -> None:
        result = MagicMock()
        result.analysis_type = MagicMock(value="price_variance")
        result.anomalies = []

        mock_repo.search.return_value = []

        mid = extractor._extract_analysis_insight(result, "u1", "s1")
        assert mid is None

    def test_detects_worsening_trend(
        self, extractor: MemoryExtractor, mock_repo: MagicMock,
    ) -> None:
        # 构造有正确 documents 结构的 anomalies
        doc = MagicMock()
        doc.po_number = ""
        doc.supplier_name = ""
        doc.invoice_number = ""
        anomalies = [MagicMock(documents=doc) for _ in range(10)]

        result = MagicMock()
        result.analysis_type = MagicMock(value="three_way_match")
        result.anomalies = anomalies  # 当前 10 条异常
        result.supplier_kpis = []
        result.summary = {}
        result.created_at = None

        # 历史平均 3 条 → 当前 10 条，变化 >30%
        mock_repo.search.return_value = [
            {"attrs": {"source_analysis_type": "three_way_match", "anomaly_count": 2}},
            {"attrs": {"source_analysis_type": "three_way_match", "anomaly_count": 3}},
            {"attrs": {"source_analysis_type": "three_way_match", "anomaly_count": 4}},
        ]

        mid = extractor._extract_analysis_insight(result, "u1", "s1")
        assert mid == "mem_001"


class TestDetectSimpleTrend:
    def test_worsening(self) -> None:
        trend = MemoryExtractor._detect_simple_trend([2, 3, 4], 10)
        assert trend is not None
        assert trend["direction"] == "worsening"

    def test_improving(self) -> None:
        trend = MemoryExtractor._detect_simple_trend([10, 8, 9], 3)
        assert trend is not None
        assert trend["direction"] == "improving"

    def test_stable_returns_none(self) -> None:
        trend = MemoryExtractor._detect_simple_trend([5, 5, 5], 5)
        assert trend is None

    def test_empty_history(self) -> None:
        trend = MemoryExtractor._detect_simple_trend([], 5)
        assert trend is None
