"""SessionSummaryExtractor — 会话摘要抽取器。

会话不活跃 30 分钟后由 idle watcher 触发：
1. 加载会话完整消息 + session_entities
2. 构造 prompt（llm_fast）生成摘要
3. 写入 memories 表（SESSION_RECAP）+ session_summaries 表 + Chroma 索引
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from config.settings import Settings
from core.time_utils import now_cn

_logger = logging.getLogger(__name__)

_SUMMARY_PROMPT = """\
你是 ERP 采购分析系统的会话摘要生成器。请总结以下对话的核心内容。

## 对话消息
{messages}

## 会话涉及的实体
{entities}

## 输出要求
返回纯 JSON（第一个字符必须是 {{），不要 markdown 代码块：
{{
  "summary_text": "<200 字以内的自然语言摘要，涵盖主要话题、关键结论和建议>",
  "key_entities": {{"vendor_id": "...", "po_number": "...", ...}},
  "tags": ["三路匹配", "SUP-001", ...],
  "conclusion": "<一句话主要结论，无则空串>",
  "confidence": 0.8
}}

规则：
- summary_text 必须简洁，突出分析结论和异常发现，不要复述对话过程
- key_entities 只包含对话中明确出现的实体编号（SUP-xxx / PO-xxx / INV-xxx 等）
- tags 用于检索命中，包含分析类型名称 + 关键实体编号 + 核心话题关键词
- confidence 反映摘要质量：0.9+ 结论明确，0.7-0.9 话题清晰但结论模糊，<0.7 对话零散"""


class SessionSummaryExtractor:
    """会话摘要抽取器（MemoryManager 内部组件）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cfg = settings.memory.session_recap

    async def extract(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """抽取会话摘要。

        Returns:
            摘要字典（含 memory_id）；跳过时返回 None。
        """
        import asyncio

        from core.observability.tracing import record_span

        with record_span("memory", "memory.session_recap.extract",
                         session_id=session_id) as span:
            try:
                result = await asyncio.wait_for(
                    self._extract_impl(session_id, user_id, span),
                    timeout=self._cfg.summary_timeout_seconds,
                )
                return result
            except asyncio.TimeoutError:
                span["status"] = "timeout"
                _logger.warning(
                    "session recap extraction timeout (%ds) for session=%s",
                    self._cfg.summary_timeout_seconds, session_id,
                )
                return None
            except Exception as exc:  # noqa: BLE001
                span["status"] = "error"
                span["error"] = str(exc)
                _logger.warning(
                    "session recap extraction failed for session=%s: %s",
                    session_id, exc,
                )
                return None

    async def _extract_impl(
        self,
        session_id: str,
        user_id: str,
        span: dict[str, Any],
    ) -> dict[str, Any] | None:
        import asyncio

        messages, entities = await asyncio.to_thread(
            self._load_session_data, session_id, user_id,
        )
        span["message_count"] = len(messages)

        if len(messages) < self._cfg.min_messages:
            span["status"] = "skipped"
            span["skip_reason"] = "too_few_messages"
            self._record_skip_span(session_id, "too_few_messages")
            _logger.info(
                "session recap skipped: session=%s messages=%d < min=%d",
                session_id, len(messages), self._cfg.min_messages,
            )
            return None

        messages_text = self._format_messages(messages)
        entities_text = json.dumps(entities, ensure_ascii=False) if entities else "{}"

        prompt = _SUMMARY_PROMPT.format(
            messages=messages_text,
            entities=entities_text,
        )

        from core.llm.model_factory import build_chat_model

        llm_fast = build_chat_model(self._settings, use_fast=True)
        response = await asyncio.to_thread(llm_fast.invoke, prompt)
        content: str = (
            response.content if hasattr(response, "content") else str(response)
        )

        parsed = self._parse_response(content)
        if parsed is None:
            span["status"] = "parse_error"
            _logger.warning(
                "session recap parse failed for session=%s: %s",
                session_id, content[:200],
            )
            return None

        confidence = float(parsed.get("confidence", 0.0))
        span["confidence"] = confidence

        summary_text = (parsed.get("summary_text") or "")[:self._cfg.summary_max_chars]
        key_entities = parsed.get("key_entities") or {}
        tags = parsed.get("tags") or []
        conclusion = parsed.get("conclusion") or ""

        memory_id = await asyncio.to_thread(
            self._write_to_stores,
            session_id=session_id,
            user_id=user_id,
            summary_text=summary_text,
            key_entities=key_entities,
            tags=tags,
            conclusion=conclusion,
            confidence=confidence,
            message_count=len(messages),
            index_to_chroma=confidence >= self._cfg.min_confidence,
        )

        span["memory_id"] = memory_id
        span["status"] = "ok"
        span["indexed_to_chroma"] = confidence >= self._cfg.min_confidence

        _logger.info(
            "session recap extracted: session=%s confidence=%.2f memory_id=%s",
            session_id, confidence, memory_id,
        )

        return {
            "session_id": session_id,
            "memory_id": memory_id,
            "summary_text": summary_text,
            "key_entities": key_entities,
            "tags": tags,
            "conclusion": conclusion,
            "confidence": confidence,
        }

    def _load_session_data(
        self,
        session_id: str,
        user_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        from core.chat.repository import ChatRepository
        from core.database.engine import get_session_factory

        sf = get_session_factory()
        repo = ChatRepository(sf)
        data = repo.get_session_with_messages(user_id, session_id)
        if data is None:
            return [], {}

        messages = data.get("messages", [])

        entities: dict[str, Any] = {}
        try:
            from core.database.engine import get_engine

            engine = get_engine(self._settings.postgresql)
            from core.memory.tables import session_entities_table

            import sqlalchemy as sa

            with engine.connect() as conn:
                row = conn.execute(
                    sa.select(session_entities_table.c.entities).where(
                        session_entities_table.c.session_id == session_id,
                    )
                ).first()
                if row:
                    entities = row.entities or {}
        except Exception as exc:  # noqa: BLE001
            _logger.warning("failed to load session_entities for %s: %s", session_id, exc)

        return messages, entities

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if content:
                lines.append(f"[{role}] {content[:500]}")
        return "\n".join(lines[-50:])

    @staticmethod
    def _parse_response(content: str) -> dict[str, Any] | None:
        raw = content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("```").strip()
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "summary_text" in data:
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    def _write_to_stores(
        self,
        *,
        session_id: str,
        user_id: str,
        summary_text: str,
        key_entities: dict[str, Any],
        tags: list[str],
        conclusion: str,
        confidence: float,
        message_count: int,
        index_to_chroma: bool,
    ) -> str | None:
        from core.memory.long_term import get_memory_repository
        from core.memory.types import MemoryType

        repo = get_memory_repository()
        expires_at = repo.compute_expires_at(MemoryType.SESSION_RECAP)

        memory_id = repo.save(
            user_id=user_id,
            session_id=session_id,
            memory_type=MemoryType.SESSION_RECAP,
            content=summary_text,
            metadata={
                "key_entities": key_entities,
                "tags": tags,
                "conclusion": conclusion,
                "confidence": confidence,
                "message_count": message_count,
            },
            expires_at=expires_at,
        )

        self._write_session_summaries(
            session_id=session_id,
            user_id=user_id,
            summary_text=summary_text,
            key_entities=key_entities,
            tags=tags,
            confidence=confidence,
            memory_id=memory_id,
            message_count=message_count,
        )

        if index_to_chroma and memory_id:
            self._index_to_chroma(
                session_id=session_id,
                user_id=user_id,
                summary_text=summary_text,
                key_entities=key_entities,
                memory_id=memory_id,
            )

        return memory_id

    def _write_session_summaries(
        self,
        *,
        session_id: str,
        user_id: str,
        summary_text: str,
        key_entities: dict[str, Any],
        tags: list[str],
        confidence: float,
        memory_id: str | None,
        message_count: int,
    ) -> None:
        try:
            from core.database.engine import get_engine
            from core.memory.tables import session_summaries_table

            import sqlalchemy as sa

            engine = get_engine(self._settings.postgresql)
            now = now_cn()

            with engine.connect() as conn:
                existing = conn.execute(
                    sa.select(session_summaries_table.c.session_id).where(
                        session_summaries_table.c.session_id == session_id,
                    )
                ).first()

                if existing:
                    conn.execute(
                        session_summaries_table.update()
                        .where(session_summaries_table.c.session_id == session_id)
                        .values(
                            summary_text=summary_text,
                            key_entities=key_entities,
                            tags=tags,
                            confidence=confidence,
                            memory_id=memory_id,
                            analysis_count=message_count,
                            updated_at=now,
                        )
                    )
                else:
                    conn.execute(
                        session_summaries_table.insert().values(
                            session_id=session_id,
                            user_id=user_id,
                            summary_text=summary_text,
                            key_entities=key_entities,
                            tags=tags,
                            confidence=confidence,
                            memory_id=memory_id,
                            analysis_count=message_count,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "session_summaries write failed for session=%s: %s",
                session_id, exc,
            )

    def _index_to_chroma(
        self,
        *,
        session_id: str,
        user_id: str,
        summary_text: str,
        key_entities: dict[str, Any],
        memory_id: str,
    ) -> None:
        try:
            from core.memory import get_chat_indexer

            indexer = get_chat_indexer()
            if indexer is None:
                return
            store = indexer._get_vector_store()
            if store is None:
                return

            collection = store._ensure_collection()
            doc_id = f"recap_{session_id}"
            metadata = {
                "user_id": user_id,
                "session_id": session_id,
                "memory_id": memory_id,
                "record_type": "session_recap",
                "key_entities": json.dumps(key_entities, ensure_ascii=False),
                "created_at": now_cn().isoformat(),
            }
            collection.upsert(
                ids=[doc_id],
                documents=[summary_text],
                metadatas=[metadata],
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "session recap chroma index failed for session=%s: %s",
                session_id, exc,
            )

    @staticmethod
    def _record_skip_span(session_id: str, reason: str) -> None:
        try:
            from core.observability.tracing import record_span

            with record_span("memory", "memory.session_recap.skip",
                             session_id=session_id, skip_reason=reason):
                pass
        except Exception:  # noqa: BLE001
            pass
