"""Tests for LLMTextExtractor and ETL Admin API routes."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.etl.models import TextRecord
from core.etl.transformers.llm_extractor import ExtraFact, LLMTextExtractor


# ============================================================
# LLMTextExtractor
# ============================================================


class TestLLMTextExtractor:
    def _make_records(self, n: int = 3) -> list[TextRecord]:
        return [
            TextRecord(
                source_node_type="PurchaseOrder",
                source_node_id=str(i),
                field_name="comments",
                text=f"Test comment {i} - urgent delivery needed",
            )
            for i in range(1, n + 1)
        ]

    def _make_llm_response(self, records: list[TextRecord]) -> str:
        return json.dumps([
            {
                "source_node_id": r.source_node_id,
                "entities": ["supplier A"],
                "anomaly_flag": True,
                "sentiment": "negative",
                "key_facts": f"Urgent delivery for record {r.source_node_id}",
            }
            for r in records
        ])

    def test_disabled_returns_empty(self):
        extractor = LLMTextExtractor(llm=MagicMock(), enabled=False)
        records = self._make_records()
        result = asyncio.get_event_loop().run_until_complete(
            extractor.extract(records)
        )
        assert result == []

    def test_empty_records_returns_empty(self):
        extractor = LLMTextExtractor(llm=MagicMock(), enabled=True)
        result = asyncio.get_event_loop().run_until_complete(
            extractor.extract([])
        )
        assert result == []

    def test_successful_extraction(self):
        records = self._make_records(2)
        mock_llm = MagicMock()
        response = MagicMock()
        response.content = self._make_llm_response(records)
        mock_llm.ainvoke = AsyncMock(return_value=response)

        extractor = LLMTextExtractor(llm=mock_llm, enabled=True, batch_size=10)
        result = asyncio.get_event_loop().run_until_complete(
            extractor.extract(records)
        )

        assert len(result) == 2
        assert all(isinstance(f, ExtraFact) for f in result)
        assert result[0].anomaly_flag is True
        assert result[0].sentiment == "negative"
        assert "Urgent" in result[0].key_facts

    def test_batching(self):
        records = self._make_records(5)
        mock_llm = MagicMock()

        # Return valid response for each batch call
        def make_response(prompt):
            resp = MagicMock()
            # Parse which records are in this batch from the prompt
            resp.content = json.dumps([
                {
                    "source_node_id": str(i),
                    "entities": [],
                    "anomaly_flag": False,
                    "sentiment": "neutral",
                    "key_facts": "ok",
                }
                for i in range(1, 6)
            ])
            return resp

        mock_llm.ainvoke = AsyncMock(side_effect=make_response)

        extractor = LLMTextExtractor(llm=mock_llm, enabled=True, batch_size=2)
        result = asyncio.get_event_loop().run_until_complete(
            extractor.extract(records)
        )

        # 5 records / batch_size 2 = 3 LLM calls
        assert mock_llm.ainvoke.await_count == 3

    def test_llm_failure_returns_empty(self):
        records = self._make_records(2)
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("API error"))

        extractor = LLMTextExtractor(llm=mock_llm, enabled=True)
        result = asyncio.get_event_loop().run_until_complete(
            extractor.extract(records)
        )
        assert result == []

    def test_invalid_json_response(self):
        records = self._make_records(1)
        mock_llm = MagicMock()
        response = MagicMock()
        response.content = "not valid json at all"
        mock_llm.ainvoke = AsyncMock(return_value=response)

        extractor = LLMTextExtractor(llm=mock_llm, enabled=True)
        result = asyncio.get_event_loop().run_until_complete(
            extractor.extract(records)
        )
        assert result == []

    def test_markdown_fenced_response(self):
        records = self._make_records(1)
        mock_llm = MagicMock()
        response = MagicMock()
        inner = json.dumps([{
            "source_node_id": "1",
            "entities": ["X"],
            "anomaly_flag": False,
            "sentiment": "positive",
            "key_facts": "All good",
        }])
        response.content = f"```json\n{inner}\n```"
        mock_llm.ainvoke = AsyncMock(return_value=response)

        extractor = LLMTextExtractor(llm=mock_llm, enabled=True)
        result = asyncio.get_event_loop().run_until_complete(
            extractor.extract(records)
        )
        assert len(result) == 1
        assert result[0].key_facts == "All good"


# ============================================================
# ExtraFact model
# ============================================================


class TestExtraFact:
    def test_defaults(self):
        f = ExtraFact(
            source_node_type="PurchaseOrder",
            source_node_id="1",
            field_name="comments",
        )
        assert f.entities == []
        assert f.anomaly_flag is False
        assert f.sentiment == "neutral"
        assert f.key_facts == ""


# ============================================================
# ETL Admin API routes
# ============================================================


class TestETLAdminRoutes:
    def test_routes_importable(self):
        from api.routes.etl import router
        routes = [r.path for r in router.routes]
        assert "/admin/etl/status" in routes
        assert "/admin/etl/trigger" in routes
