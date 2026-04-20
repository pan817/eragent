"""OpenAI-compatible LLM client for Graphiti that works with Qwen/Dashscope.

graphiti-core's default ``OpenAIClient`` uses the ``responses.parse()`` API
for structured output, which is not supported by Dashscope/Qwen. This client
falls back to ``chat.completions.create()`` with ``json_object`` response
format and manual Pydantic validation, making it compatible with any
OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
from core.logging_utils import get_logger
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_base_client import BaseOpenAIClient

_logger = get_logger(__name__)


class QwenCompatibleClient(BaseOpenAIClient):
    """OpenAI-compatible client that avoids ``responses.parse()`` API.

    Uses ``chat.completions.create()`` with JSON mode for all requests,
    including structured output. This makes it compatible with Qwen,
    DeepSeek, and other OpenAI-compatible endpoints that don't support
    the ``responses`` API.
    """

    def __init__(self, config: LLMConfig | None = None, client: AsyncOpenAI | None = None):
        super().__init__(config)
        if client is not None:
            self.client = client
        else:
            cfg = config or LLMConfig()
            self.client = AsyncOpenAI(
                api_key=cfg.api_key,
                base_url=cfg.base_url,
            )

    async def _create_structured_completion(
        self,
        model: str,
        messages: list[ChatCompletionMessageParam],
        temperature: float | None,
        max_tokens: int,
        response_model: type[BaseModel],
        reasoning: str | None = None,
        verbosity: str | None = None,
    ) -> Any:
        """Override: use chat.completions + JSON mode instead of responses.parse.

        Appends the JSON schema to the system prompt so the LLM knows the
        expected format, then validates the response with Pydantic.
        """
        schema = response_model.model_json_schema()
        schema_instruction = (
            f"\n\nYou MUST respond with a JSON object that conforms to this schema:\n"
            f"```json\n{json.dumps(schema, ensure_ascii=False)}\n```\n"
            f"Return ONLY the JSON object, no markdown fences or extra text."
        )

        # Inject schema into the last user message or system message
        augmented_messages = list(messages)
        if augmented_messages and augmented_messages[0].get("role") == "system":
            augmented_messages[0] = {
                **augmented_messages[0],
                "content": augmented_messages[0]["content"] + schema_instruction,
            }
        else:
            augmented_messages.insert(0, {
                "role": "system",
                "content": f"Respond in JSON format.{schema_instruction}",
            })

        response = await self.client.chat.completions.create(
            model=model,
            messages=augmented_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )

        # Wrap in a compatible object for _handle_structured_response
        content = response.choices[0].message.content or "{}"

        # Strip markdown fences if present
        text = content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

        # Validate with Pydantic
        try:
            parsed = response_model.model_validate_json(text)
            validated_json = parsed.model_dump_json()
        except Exception as e:
            _logger.warning(
                "Structured output validation failed, attempting field mapping: %s",
                e,
            )
            # Try field mapping for common mismatches
            raw = json.loads(text)
            raw = self._remap_fields(raw, response_model)
            parsed = response_model.model_validate(raw)
            validated_json = parsed.model_dump_json()

        # Return a mock response object compatible with _handle_structured_response
        return _StructuredResponse(
            output_text=validated_json,
            usage=response.usage,
        )

    async def _create_completion(
        self,
        model: str,
        messages: list[ChatCompletionMessageParam],
        temperature: float | None,
        max_tokens: int,
        response_model: type[BaseModel] | None = None,
        reasoning: str | None = None,
        verbosity: str | None = None,
    ) -> Any:
        """Regular chat completion with JSON format."""
        return await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )

    @staticmethod
    def _remap_fields(raw: Any, model: type[BaseModel]) -> Any:
        """Attempt to remap common field name mismatches.

        Handles cases like Qwen returning ``entity`` instead of ``name``,
        or ``entities`` instead of ``extracted_entities``.
        """
        if isinstance(raw, list):
            # Model expects a wrapper object
            schema = model.model_json_schema()
            props = schema.get("properties", {})
            for field_name, field_info in props.items():
                if field_info.get("type") == "array":
                    return {field_name: [_remap_entity(item) for item in raw]}
            return raw

        if isinstance(raw, dict):
            # Remap top-level keys
            schema = model.model_json_schema()
            props = schema.get("properties", {})
            remapped = {}
            for field_name in props:
                if field_name in raw:
                    val = raw[field_name]
                elif field_name == "extracted_entities" and "entities" in raw:
                    val = raw["entities"]
                else:
                    val = raw.get(field_name)

                if isinstance(val, list):
                    val = [_remap_entity(item) if isinstance(item, dict) else item for item in val]
                remapped[field_name] = val
            return remapped

        return raw


def _remap_entity(item: dict[str, Any]) -> dict[str, Any]:
    """Remap entity dict fields: entity→name, type→entity_type_id, etc."""
    remapped = dict(item)
    if "name" not in remapped and "entity" in remapped:
        remapped["name"] = remapped.pop("entity")
    if "entity_type_id" not in remapped and "type" in remapped:
        remapped["entity_type_id"] = remapped.pop("type")
    return remapped


class _StructuredResponse:
    """Minimal response object compatible with ``_handle_structured_response``."""

    def __init__(self, output_text: str, usage: Any = None):
        self.output_text = output_text
        self.usage = usage


# ── Dashscope-compatible Embedder ─────────────────────────────


_DASHSCOPE_EMBED_BATCH_LIMIT = 10


class DashscopeCompatibleEmbedder:
    """OpenAI embedder that auto-splits batches for Dashscope's 10-item limit.

    Dashscope's ``text-embedding-v3`` (and similar models) reject
    ``embeddings.create`` calls with more than 10 inputs. This wrapper
    transparently chunks large batches and concatenates the results.

    Inherits from ``EmbedderClient`` (ABC) so Graphiti's Pydantic
    ``GraphitiClients`` model accepts it via ``isinstance`` check.
    """

    def __init__(self, inner_embedder: Any) -> None:
        self._inner = inner_embedder

    async def create(self, input_data: Any) -> list[float] | list[list[float]]:
        """Create embeddings, auto-splitting if batch > 10."""
        # Single string → delegate directly
        if isinstance(input_data, str):
            return await self._inner.create(input_data=input_data)

        data_list = list(input_data)
        if len(data_list) <= _DASHSCOPE_EMBED_BATCH_LIMIT:
            return await self._inner.create(input_data=data_list)

        all_embeddings: list[list[float]] = []
        for i in range(0, len(data_list), _DASHSCOPE_EMBED_BATCH_LIMIT):
            chunk = data_list[i : i + _DASHSCOPE_EMBED_BATCH_LIMIT]
            batch_result = await self._inner.create(input_data=chunk)
            if isinstance(batch_result, list) and batch_result and isinstance(batch_result[0], list):
                all_embeddings.extend(batch_result)
            else:
                all_embeddings.append(batch_result)
        return all_embeddings

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        """Batch create, auto-splitting for Dashscope limit."""
        if len(input_data_list) <= _DASHSCOPE_EMBED_BATCH_LIMIT:
            return await self._inner.create_batch(input_data_list)

        all_embeddings: list[list[float]] = []
        for i in range(0, len(input_data_list), _DASHSCOPE_EMBED_BATCH_LIMIT):
            chunk = input_data_list[i : i + _DASHSCOPE_EMBED_BATCH_LIMIT]
            batch_result = await self._inner.create_batch(chunk)
            all_embeddings.extend(batch_result)
        return all_embeddings

    # Forward any other attribute access to the inner embedder
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
