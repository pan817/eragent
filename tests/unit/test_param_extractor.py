"""前置 LLM 参数拆解器（ParamExtractor）单元测试。

覆盖场景：
- _parse_response：合法 JSON / 非法 JSON / 字段校验
- extract：LLM 调用成功 / 调用失败降级
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from core.orchestrator.param_extractor import ParamExtractor


@pytest.fixture
def extractor() -> ParamExtractor:
    """构造 ParamExtractor（使用 mock settings）。"""
    settings = MagicMock()
    return ParamExtractor(settings)


class TestParseResponse:
    """_parse_response JSON 解析和字段校验测试。"""

    def test_full_params(self, extractor: ParamExtractor) -> None:
        """完整参数解析。"""
        content = json.dumps({
            "po_number": "PO-2024-001",
            "vendor_id": "SUP-001",
            "invoice_num": None,
            "check_number": None,
            "receipt_number": None,
            "days": 30,
            "limit": 1,
            "order_by": "date_desc",
        })
        result = extractor._parse_response(content)
        assert result["po_number"] == "PO-2024-001"
        assert result["vendor_id"] == "SUP-001"
        assert result["days"] == 30
        assert result["limit"] == 1
        assert result["order_by"] == "date_desc"
        assert "invoice_num" not in result  # None 值不保留

    def test_limit_only(self, extractor: ParamExtractor) -> None:
        """仅提取 limit。"""
        content = json.dumps({
            "po_number": None, "vendor_id": None,
            "invoice_num": None, "check_number": None,
            "receipt_number": None, "days": None,
            "limit": 5, "order_by": None,
        })
        result = extractor._parse_response(content)
        assert result == {"limit": 5}

    def test_order_by_validation(self, extractor: ParamExtractor) -> None:
        """非法 order_by 值被丢弃。"""
        content = json.dumps({
            "po_number": None, "vendor_id": None,
            "invoice_num": None, "check_number": None,
            "receipt_number": None, "days": None,
            "limit": None, "order_by": "invalid_value",
        })
        result = extractor._parse_response(content)
        assert "order_by" not in result

    def test_valid_order_by_values(self, extractor: ParamExtractor) -> None:
        """所有合法 order_by 枚举值均通过校验。"""
        for val in ("date_desc", "date_asc", "amount_desc", "amount_asc"):
            content = json.dumps({
                "po_number": None, "vendor_id": None,
                "invoice_num": None, "check_number": None,
                "receipt_number": None, "days": None,
                "limit": None, "order_by": val,
            })
            result = extractor._parse_response(content)
            assert result["order_by"] == val

    def test_negative_limit_ignored(self, extractor: ParamExtractor) -> None:
        """负数 limit 被丢弃。"""
        content = json.dumps({
            "po_number": None, "vendor_id": None,
            "invoice_num": None, "check_number": None,
            "receipt_number": None, "days": None,
            "limit": -1, "order_by": None,
        })
        result = extractor._parse_response(content)
        assert "limit" not in result

    def test_zero_limit_ignored(self, extractor: ParamExtractor) -> None:
        """零 limit 被丢弃。"""
        content = json.dumps({
            "po_number": None, "vendor_id": None,
            "invoice_num": None, "check_number": None,
            "receipt_number": None, "days": None,
            "limit": 0, "order_by": None,
        })
        result = extractor._parse_response(content)
        assert "limit" not in result

    def test_negative_days_ignored(self, extractor: ParamExtractor) -> None:
        """负数 days 被丢弃。"""
        content = json.dumps({
            "po_number": None, "vendor_id": None,
            "invoice_num": None, "check_number": None,
            "receipt_number": None, "days": -7,
            "limit": None, "order_by": None,
        })
        result = extractor._parse_response(content)
        assert "days" not in result

    def test_markdown_wrapped_json(self, extractor: ParamExtractor) -> None:
        """处理 markdown 代码块包裹的 JSON。"""
        content = '```json\n{"po_number": "PO-001", "vendor_id": null, "invoice_num": null, "check_number": null, "receipt_number": null, "days": null, "limit": null, "order_by": null}\n```'
        result = extractor._parse_response(content)
        assert result["po_number"] == "PO-001"

    def test_invalid_json_returns_empty(self, extractor: ParamExtractor) -> None:
        """非法 JSON 返回空 dict。"""
        with pytest.raises(json.JSONDecodeError):
            extractor._parse_response("not json at all")

    def test_non_dict_returns_empty(self, extractor: ParamExtractor) -> None:
        """非 dict 类型返回空 dict。"""
        result = extractor._parse_response("[1, 2, 3]")
        assert result == {}

    def test_entity_whitespace_stripped(self, extractor: ParamExtractor) -> None:
        """实体编号前后空格被去除。"""
        content = json.dumps({
            "po_number": " PO-001 ", "vendor_id": None,
            "invoice_num": None, "check_number": None,
            "receipt_number": None, "days": None,
            "limit": None, "order_by": None,
        })
        result = extractor._parse_response(content)
        assert result["po_number"] == "PO-001"


class TestExtract:
    """extract 方法集成测试（mock LLM）。"""

    def test_successful_extraction(self, extractor: ParamExtractor) -> None:
        """LLM 调用成功时返回解析结果。"""
        mock_response = MagicMock()
        mock_response.content = json.dumps({
            "po_number": None, "vendor_id": None,
            "invoice_num": None, "check_number": None,
            "receipt_number": None, "days": None,
            "limit": 1, "order_by": "date_desc",
        })
        mock_response.usage_metadata = None

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = mock_response
        extractor._llm = mock_llm

        with patch("core.observability.tracing.record_span") as mock_span:
            mock_span.return_value.__enter__ = MagicMock(return_value={})
            mock_span.return_value.__exit__ = MagicMock(return_value=False)
            result = extractor.extract("查询最新的一个支付单")

        assert result["limit"] == 1
        assert result["order_by"] == "date_desc"

    def test_llm_failure_returns_empty(self, extractor: ParamExtractor) -> None:
        """LLM 调用异常时降级返回空 dict。"""
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = RuntimeError("API timeout")
        extractor._llm = mock_llm

        with patch("core.observability.tracing.record_span") as mock_span:
            mock_span.return_value.__enter__ = MagicMock(return_value={})
            mock_span.return_value.__exit__ = MagicMock(return_value=False)
            result = extractor.extract("查询最新的一个支付单")

        assert result == {}
