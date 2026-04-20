"""LLM-based free-text extraction for ERP comments / descriptions.

Extracts structured business facts (entities, anomalies, sentiment) from
free-text fields like PO comments, invoice descriptions, and contract notes.
Uses ``llm_fast`` to keep costs low.  Disabled by default — enable via
``graphiti_etl.llm_extraction_enabled``.
"""

from __future__ import annotations

import json
from core.logging_utils import get_logger
from dataclasses import dataclass, field
from itertools import islice
from typing import Any

from core.etl.models import TextRecord

_logger = get_logger(__name__)

_EXTRACTION_PROMPT = """\
You are an ERP data analyst. Extract structured business facts from the
following ERP text fields. For each record, identify:

1. **entities**: mentioned suppliers, materials, amounts, dates
2. **anomaly_flag**: true if the text mentions problems, risks, urgency
3. **sentiment**: positive / neutral / negative
4. **key_facts**: 1-3 sentence summary of important business information

Input records (JSON array):
{records_json}

Respond with a JSON array of objects, one per input record, each having:
{{"source_node_id": "...", "entities": [...], "anomaly_flag": bool, "sentiment": "...", "key_facts": "..."}}

Return ONLY the JSON array, no markdown fences or extra text."""


@dataclass
class ExtraFact:
    """A business fact extracted by LLM from free text."""

    source_node_type: str
    source_node_id: str
    field_name: str
    entities: list[str] = field(default_factory=list)
    anomaly_flag: bool = False
    sentiment: str = "neutral"
    key_facts: str = ""


def _batched(iterable: Any, n: int) -> Any:
    """Yield successive n-sized chunks from iterable."""
    it = iter(iterable)
    while True:
        batch = list(islice(it, n))
        if not batch:
            break
        yield batch


class LLMTextExtractor:
    """Extract structured facts from free-text ERP fields via LLM."""

    def __init__(
        self,
        llm: Any,
        enabled: bool = True,
        batch_size: int = 20,
    ) -> None:
        self._llm = llm
        self._enabled = enabled
        self._batch_size = batch_size

    async def extract(self, records: list[TextRecord]) -> list[ExtraFact]:
        """Batch-extract facts from *records*.

        Returns an empty list immediately if the extractor is disabled or
        *records* is empty.
        """
        if not self._enabled or not records:
            return []

        results: list[ExtraFact] = []
        for batch in _batched(records, self._batch_size):
            facts = await self._call_llm(batch)
            results.extend(facts)
        return results

    async def _call_llm(self, batch: list[TextRecord]) -> list[ExtraFact]:
        """Call the LLM for a single batch and parse the response."""
        records_json = json.dumps(
            [
                {
                    "source_node_id": r.source_node_id,
                    "source_node_type": r.source_node_type,
                    "field_name": r.field_name,
                    "text": r.text[:500],  # truncate to control token usage
                }
                for r in batch
            ],
            ensure_ascii=False,
        )

        prompt = _EXTRACTION_PROMPT.format(records_json=records_json)

        try:
            response = await self._llm.ainvoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)
            parsed = self._parse_response(content, batch)
            _logger.info(
                "LLM extraction: %d records → %d facts", len(batch), len(parsed)
            )
            return parsed
        except Exception:
            _logger.warning(
                "LLM extraction failed for batch of %d records",
                len(batch),
                exc_info=True,
            )
            return []

    @staticmethod
    def _parse_response(
        content: str, batch: list[TextRecord]
    ) -> list[ExtraFact]:
        """Parse LLM JSON response into ExtraFact objects."""
        # Strip markdown fences if present
        text = content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            _logger.warning("LLM response is not valid JSON: %s", text[:200])
            return []

        if not isinstance(items, list):
            items = [items]

        # Build lookup for source metadata
        source_map = {r.source_node_id: r for r in batch}

        facts: list[ExtraFact] = []
        for item in items:
            node_id = str(item.get("source_node_id", ""))
            source = source_map.get(node_id)
            if source is None:
                continue
            facts.append(
                ExtraFact(
                    source_node_type=source.source_node_type,
                    source_node_id=node_id,
                    field_name=source.field_name,
                    entities=item.get("entities", []),
                    anomaly_flag=bool(item.get("anomaly_flag", False)),
                    sentiment=item.get("sentiment", "neutral"),
                    key_facts=item.get("key_facts", ""),
                )
            )
        return facts
