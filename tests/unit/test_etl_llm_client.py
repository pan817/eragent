"""Tests for core.etl.llm_client — QwenCompatibleClient & DashscopeCompatibleEmbedder."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from core.etl.llm_client import (
    DashscopeCompatibleEmbedder,
    QwenCompatibleClient,
    _StructuredResponse,
    _remap_entity,
)


# ── Helper Models ────────────────────────────────────────────


class SampleModel(BaseModel):
    name: str
    score: float


class EntityListModel(BaseModel):
    extracted_entities: list[dict[str, Any]]


class SimpleListModel(BaseModel):
    items: list[str]


# ── _StructuredResponse ─────────────────────────────────────


class TestStructuredResponse:
    def test_attributes(self) -> None:
        usage = MagicMock()
        resp = _StructuredResponse(output_text='{"a":1}', usage=usage)
        assert resp.output_text == '{"a":1}'
        assert resp.usage is usage

    def test_none_usage(self) -> None:
        resp = _StructuredResponse(output_text="{}")
        assert resp.usage is None


# ── _remap_entity ────────────────────────────────────────────


class TestRemapEntity:
    def test_entity_to_name(self) -> None:
        result = _remap_entity({"entity": "Acme", "score": 10})
        assert result["name"] == "Acme"
        assert "entity" not in result

    def test_type_to_entity_type_id(self) -> None:
        result = _remap_entity({"type": "Supplier", "name": "X"})
        assert result["entity_type_id"] == "Supplier"
        assert "type" not in result

    def test_no_remap_when_keys_present(self) -> None:
        original = {"name": "Acme", "entity_type_id": "Supplier", "entity": "ignored"}
        result = _remap_entity(original)
        assert result["name"] == "Acme"
        assert result["entity_type_id"] == "Supplier"
        # "entity" key kept because "name" already present
        assert result["entity"] == "ignored"

    def test_passthrough_unrelated_fields(self) -> None:
        result = _remap_entity({"foo": "bar"})
        assert result == {"foo": "bar"}


# ── QwenCompatibleClient ────────────────────────────────────


class TestQwenCompatibleClientInit:
    def test_init_with_custom_client(self) -> None:
        mock_client = MagicMock()
        qc = QwenCompatibleClient(client=mock_client)
        assert qc.client is mock_client

    def test_init_without_client_creates_async_openai(self) -> None:
        with patch("core.etl.llm_client.AsyncOpenAI") as mock_cls:
            from graphiti_core.llm_client.config import LLMConfig

            cfg = LLMConfig(api_key="test-key", base_url="https://example.com/v1")
            QwenCompatibleClient(config=cfg)
            mock_cls.assert_called_once_with(api_key="test-key", base_url="https://example.com/v1")

    def test_init_no_args_uses_default_config(self) -> None:
        with patch("core.etl.llm_client.AsyncOpenAI"):
            qc = QwenCompatibleClient()
            assert qc.client is not None


class TestCreateStructuredCompletion:
    def _make_client(self) -> tuple[QwenCompatibleClient, AsyncMock]:
        mock_openai = AsyncMock()
        client = QwenCompatibleClient(client=mock_openai)
        return client, mock_openai

    def _make_chat_response(self, content: str) -> MagicMock:
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content
        resp.usage = MagicMock()
        return resp

    def test_structured_completion_with_system_message(self) -> None:
        client, mock_openai = self._make_client()
        payload = SampleModel(name="test", score=1.5).model_dump_json()
        mock_openai.chat.completions.create = AsyncMock(
            return_value=self._make_chat_response(payload)
        )

        messages = [
            {"role": "system", "content": "You are a helper."},
            {"role": "user", "content": "Extract data."},
        ]

        result = asyncio.get_event_loop().run_until_complete(
            client._create_structured_completion(
                model="qwen-max",
                messages=messages,
                temperature=0.0,
                max_tokens=1000,
                response_model=SampleModel,
            )
        )

        assert isinstance(result, _StructuredResponse)
        parsed = json.loads(result.output_text)
        assert parsed["name"] == "test"
        assert parsed["score"] == 1.5

        # Verify schema was injected into system message
        call_args = mock_openai.chat.completions.create.call_args
        sent_messages = call_args.kwargs["messages"]
        assert "JSON" in sent_messages[0]["content"]

    def test_structured_completion_without_system_message(self) -> None:
        client, mock_openai = self._make_client()
        payload = SampleModel(name="x", score=0.0).model_dump_json()
        mock_openai.chat.completions.create = AsyncMock(
            return_value=self._make_chat_response(payload)
        )

        messages = [{"role": "user", "content": "Do something."}]

        result = asyncio.get_event_loop().run_until_complete(
            client._create_structured_completion(
                model="qwen-max",
                messages=messages,
                temperature=None,
                max_tokens=500,
                response_model=SampleModel,
            )
        )

        assert isinstance(result, _StructuredResponse)
        # A system message should have been inserted
        call_args = mock_openai.chat.completions.create.call_args
        sent_messages = call_args.kwargs["messages"]
        assert sent_messages[0]["role"] == "system"
        assert "Respond in JSON format" in sent_messages[0]["content"]

    def test_structured_completion_strips_markdown_fences(self) -> None:
        client, mock_openai = self._make_client()
        inner = SampleModel(name="fenced", score=9.9).model_dump_json()
        fenced = f"```json\n{inner}\n```"
        mock_openai.chat.completions.create = AsyncMock(
            return_value=self._make_chat_response(fenced)
        )

        result = asyncio.get_event_loop().run_until_complete(
            client._create_structured_completion(
                model="qwen-max",
                messages=[{"role": "user", "content": "test"}],
                temperature=0.0,
                max_tokens=500,
                response_model=SampleModel,
            )
        )

        parsed = json.loads(result.output_text)
        assert parsed["name"] == "fenced"

    def test_structured_completion_null_content_uses_empty_dict(self) -> None:
        client, mock_openai = self._make_client()
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = None
        resp.usage = MagicMock()
        mock_openai.chat.completions.create = AsyncMock(return_value=resp)

        # "{}" won't validate to SampleModel (missing required fields),
        # so expect validation error propagation
        with pytest.raises(Exception):
            asyncio.get_event_loop().run_until_complete(
                client._create_structured_completion(
                    model="qwen-max",
                    messages=[{"role": "user", "content": "test"}],
                    temperature=0.0,
                    max_tokens=500,
                    response_model=SampleModel,
                )
            )

    def test_structured_completion_remap_fallback(self) -> None:
        """When direct validation fails, _remap_fields is used as fallback."""
        client, mock_openai = self._make_client()
        # Return "entities" instead of "extracted_entities"
        raw = json.dumps({"entities": [{"entity": "Acme", "type": "Supplier"}]})
        mock_openai.chat.completions.create = AsyncMock(
            return_value=self._make_chat_response(raw)
        )

        result = asyncio.get_event_loop().run_until_complete(
            client._create_structured_completion(
                model="qwen-max",
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
                temperature=0.0,
                max_tokens=500,
                response_model=EntityListModel,
            )
        )

        parsed = json.loads(result.output_text)
        assert "extracted_entities" in parsed
        entity = parsed["extracted_entities"][0]
        assert entity["name"] == "Acme"
        assert entity["entity_type_id"] == "Supplier"


class TestCreateCompletion:
    def test_delegates_to_chat_completions(self) -> None:
        mock_openai = AsyncMock()
        expected_resp = MagicMock()
        mock_openai.chat.completions.create = AsyncMock(return_value=expected_resp)
        client = QwenCompatibleClient(client=mock_openai)

        messages = [{"role": "user", "content": "hello"}]
        result = asyncio.get_event_loop().run_until_complete(
            client._create_completion(
                model="qwen-max",
                messages=messages,
                temperature=0.5,
                max_tokens=200,
            )
        )

        assert result is expected_resp
        mock_openai.chat.completions.create.assert_awaited_once_with(
            model="qwen-max",
            messages=messages,
            temperature=0.5,
            max_tokens=200,
            response_format={"type": "json_object"},
        )


# ── _remap_fields (static method) ───────────────────────────


class TestRemapFields:
    def test_list_input_wraps_into_array_field(self) -> None:
        raw = [{"entity": "A"}, {"entity": "B"}]
        result = QwenCompatibleClient._remap_fields(raw, SimpleListModel)
        assert "items" in result
        assert len(result["items"]) == 2

    def test_dict_input_remaps_entities_key(self) -> None:
        raw = {"entities": [{"entity": "X", "type": "Vendor"}]}
        result = QwenCompatibleClient._remap_fields(raw, EntityListModel)
        assert "extracted_entities" in result
        ent = result["extracted_entities"][0]
        assert ent["name"] == "X"
        assert ent["entity_type_id"] == "Vendor"

    def test_dict_input_keeps_matching_keys(self) -> None:
        raw = {"extracted_entities": [{"name": "Y"}]}
        result = QwenCompatibleClient._remap_fields(raw, EntityListModel)
        assert result["extracted_entities"][0]["name"] == "Y"

    def test_dict_with_missing_key_returns_none(self) -> None:
        raw = {}
        result = QwenCompatibleClient._remap_fields(raw, EntityListModel)
        assert result["extracted_entities"] is None

    def test_passthrough_non_dict_non_list(self) -> None:
        assert QwenCompatibleClient._remap_fields("hello", SampleModel) == "hello"
        assert QwenCompatibleClient._remap_fields(42, SampleModel) == 42


# ── DashscopeCompatibleEmbedder ─────────────────────────────


class TestDashscopeCompatibleEmbedder:
    def test_create_single_string_delegates(self) -> None:
        inner = AsyncMock()
        inner.create = AsyncMock(return_value=[0.1, 0.2])
        embedder = DashscopeCompatibleEmbedder(inner)

        result = asyncio.get_event_loop().run_until_complete(
            embedder.create("hello world")
        )

        assert result == [0.1, 0.2]
        inner.create.assert_awaited_once_with(input_data="hello world")

    def test_create_small_batch_delegates(self) -> None:
        inner = AsyncMock()
        inner.create = AsyncMock(return_value=[[0.1], [0.2]])
        embedder = DashscopeCompatibleEmbedder(inner)

        data = ["a", "b"]
        result = asyncio.get_event_loop().run_until_complete(
            embedder.create(data)
        )

        assert result == [[0.1], [0.2]]
        inner.create.assert_awaited_once_with(input_data=["a", "b"])

    def test_create_large_batch_splits(self) -> None:
        inner = AsyncMock()
        # Each chunk returns a list of embeddings (list of lists)
        inner.create = AsyncMock(side_effect=[
            [[float(i)] for i in range(10)],
            [[float(i)] for i in range(10, 15)],
        ])
        embedder = DashscopeCompatibleEmbedder(inner)

        data = [f"text_{i}" for i in range(15)]
        result = asyncio.get_event_loop().run_until_complete(
            embedder.create(data)
        )

        assert len(result) == 15
        assert inner.create.await_count == 2

    def test_create_large_batch_single_embedding_per_chunk(self) -> None:
        """When inner.create returns a flat list (single embedding), it gets appended."""
        inner = AsyncMock()
        # Return flat list (not list-of-lists) for each chunk
        inner.create = AsyncMock(side_effect=[
            [0.1, 0.2],  # flat
            [0.3, 0.4],  # flat
        ])
        embedder = DashscopeCompatibleEmbedder(inner)

        data = [f"text_{i}" for i in range(20)]
        result = asyncio.get_event_loop().run_until_complete(
            embedder.create(data)
        )

        assert inner.create.await_count == 2
        # Each flat result is appended as a single embedding
        assert result == [[0.1, 0.2], [0.3, 0.4]]

    def test_create_batch_small(self) -> None:
        inner = AsyncMock()
        inner.create_batch = AsyncMock(return_value=[[0.1], [0.2]])
        embedder = DashscopeCompatibleEmbedder(inner)

        result = asyncio.get_event_loop().run_until_complete(
            embedder.create_batch(["a", "b"])
        )

        assert result == [[0.1], [0.2]]
        inner.create_batch.assert_awaited_once()

    def test_create_batch_large_splits(self) -> None:
        inner = AsyncMock()
        inner.create_batch = AsyncMock(side_effect=[
            [[float(i)] for i in range(10)],
            [[float(i)] for i in range(10, 12)],
        ])
        embedder = DashscopeCompatibleEmbedder(inner)

        data = [f"text_{i}" for i in range(12)]
        result = asyncio.get_event_loop().run_until_complete(
            embedder.create_batch(data)
        )

        assert len(result) == 12
        assert inner.create_batch.await_count == 2

    def test_getattr_forwards_to_inner(self) -> None:
        inner = MagicMock()
        inner.some_attr = "value"
        embedder = DashscopeCompatibleEmbedder(inner)
        assert embedder.some_attr == "value"

    def test_create_exactly_10_items_no_split(self) -> None:
        inner = AsyncMock()
        inner.create = AsyncMock(return_value=[[0.1]] * 10)
        embedder = DashscopeCompatibleEmbedder(inner)

        data = [f"t_{i}" for i in range(10)]
        result = asyncio.get_event_loop().run_until_complete(
            embedder.create(data)
        )

        assert len(result) == 10
        inner.create.assert_awaited_once()

    def test_create_batch_exactly_10_no_split(self) -> None:
        inner = AsyncMock()
        inner.create_batch = AsyncMock(return_value=[[0.1]] * 10)
        embedder = DashscopeCompatibleEmbedder(inner)

        result = asyncio.get_event_loop().run_until_complete(
            embedder.create_batch([f"t_{i}" for i in range(10)])
        )

        assert len(result) == 10
        inner.create_batch.assert_awaited_once()

    def test_create_11_items_splits_into_two(self) -> None:
        inner = AsyncMock()
        inner.create = AsyncMock(side_effect=[
            [[float(i)] for i in range(10)],
            [[10.0]],
        ])
        embedder = DashscopeCompatibleEmbedder(inner)

        result = asyncio.get_event_loop().run_until_complete(
            embedder.create([f"t_{i}" for i in range(11)])
        )

        assert len(result) == 11
        assert inner.create.await_count == 2
