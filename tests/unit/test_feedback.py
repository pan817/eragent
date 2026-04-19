"""FeedbackDetector 单元测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.memory.feedback import FeedbackDetector


@pytest.fixture
def detector() -> FeedbackDetector:
    return FeedbackDetector()


class TestDetectSignal:
    def test_correction_signal(self, detector: FeedbackDetector) -> None:
        assert detector.detect_signal("这个价格差异不对，是因为批量折扣") == "correction"

    def test_correction_false_positive(self, detector: FeedbackDetector) -> None:
        assert detector.detect_signal("PO-10045 不应该标记为异常") == "correction"

    def test_preference_signal(self, detector: FeedbackDetector) -> None:
        assert detector.detect_signal("以后都用表格输出") == "preference"

    def test_fact_signal(self, detector: FeedbackDetector) -> None:
        assert detector.detect_signal("我们公司的匹配容差是 2%") == "fact"

    def test_no_signal(self, detector: FeedbackDetector) -> None:
        assert detector.detect_signal("分析一下三路匹配") is None

    def test_no_signal_pure_query(self, detector: FeedbackDetector) -> None:
        assert detector.detect_signal("查最新的 PO") is None

    def test_priority_correction_over_preference(self, detector: FeedbackDetector) -> None:
        # correction 关键词优先于 preference
        result = detector.detect_signal("这不对，以后不要这样标记")
        assert result == "correction"


class TestParseResponse:
    def test_valid_correction(self) -> None:
        raw = json.dumps({
            "type": "correction",
            "content": "PO-10045 价格差异是批量折扣",
            "related_entities": {"po_number": "PO-10045"},
            "confidence": 0.85,
        })
        result = FeedbackDetector._parse_response(raw)
        assert result is not None
        assert result["type"] == "correction"
        assert result["confidence"] == 0.85

    def test_type_none_returns_none(self) -> None:
        raw = json.dumps({"type": "none", "content": "", "confidence": 0.9})
        assert FeedbackDetector._parse_response(raw) is None

    def test_empty_content_returns_none(self) -> None:
        raw = json.dumps({"type": "correction", "content": "", "confidence": 0.9})
        assert FeedbackDetector._parse_response(raw) is None

    def test_invalid_json_returns_none(self) -> None:
        assert FeedbackDetector._parse_response("not json") is None

    def test_markdown_code_block(self) -> None:
        raw = "```json\n" + json.dumps({
            "type": "user_preference",
            "content": "用户偏好表格输出",
            "confidence": 0.8,
        }) + "\n```"
        result = FeedbackDetector._parse_response(raw)
        assert result is not None
        assert result["type"] == "user_preference"


class TestExtract:
    @pytest.mark.asyncio
    async def test_successful_extraction(self, detector: FeedbackDetector) -> None:
        llm_fast = AsyncMock()
        response = MagicMock()
        response.content = json.dumps({
            "type": "correction",
            "content": "测试内容",
            "related_entities": {},
            "confidence": 0.9,
        })
        llm_fast.ainvoke.return_value = response

        result = await detector.extract(
            "这不对", {"context_summary": "分析结果摘要"}, "correction", llm_fast,
        )
        assert result is not None
        assert result["type"] == "correction"

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self, detector: FeedbackDetector) -> None:
        llm_fast = AsyncMock()
        llm_fast.ainvoke.side_effect = RuntimeError("LLM timeout")

        result = await detector.extract(
            "这不对", {}, "correction", llm_fast,
        )
        assert result is None
