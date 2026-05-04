"""MemoryManager — 记忆系统统一入口。

Orchestrator 只通过此类与记忆系统交互。
内部协调短期记忆、长期记忆、提取、检测、整合、摘要等组件。
所有异步操作在内部 fire-and-forget，不向调用方泄露。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from config.settings import Settings

_logger = logging.getLogger(__name__)


class MemoryManager:
    """记忆系统统一入口。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        # 短期记忆（委托现有 ShortTermMemory）
        from core.memory.short_term import ShortTermMemory

        self._short_term = ShortTermMemory(settings=settings)

        # 懒初始化的内部组件
        self._feedback_detector: Any = None
        self._chat_indexer: Any = None
        self._session_summarizer: Any = None

    # ── 分析前：构建记忆上下文 ──────────────────────────────

    async def build_context(
        self,
        user_id: str,
        query: str,
        parsed_params: dict[str, Any],
        analysis_type: str | None = None,
    ) -> str:
        """检索长期记忆并格式化为 prompt 注入文本。

        同步执行（结果要注入分析 prompt），带超时保护。
        超时或失败返回空字符串，不阻塞分析。
        """
        if not self._settings.memory.long_term_enabled:
            return ""

        from core.observability.tracing import record_span

        mem_trace_id = f"mem_{uuid.uuid4().hex[:12]}"

        with record_span("memory", "memory_ref", memory_trace_id=mem_trace_id) as ref_span:
            try:
                timeout = self._settings.memory.long_term_search_timeout_seconds
                result = await asyncio.wait_for(
                    self._search_and_format(user_id, query, parsed_params, analysis_type),
                    timeout=timeout,
                )
                ref_span["memory_context_chars"] = len(result)
                ref_span["status"] = "ok"
                return result
            except asyncio.TimeoutError:
                ref_span["status"] = "timeout"
                _logger.warning(
                    "memory search timeout (%.1fs), proceeding without memory context",
                    self._settings.memory.long_term_search_timeout_seconds,
                )
                return ""
            except Exception as exc:  # noqa: BLE001
                ref_span["status"] = "error"
                ref_span["error"] = str(exc)
                _logger.warning("memory context build failed: %s", exc)
                return ""

    async def _search_and_format(
        self,
        user_id: str,
        query: str,
        parsed_params: dict[str, Any],
        analysis_type: str | None,
    ) -> str:
        """内部方法：检索 + 格式化。"""
        entity_ids = [
            v for k, v in parsed_params.items()
            if k in ("vendor_id", "po_number", "invoice_num") and v
        ]

        from core.memory.injection import format_memory_injection
        from core.memory.long_term import get_memory_repository

        repo = get_memory_repository()
        memories = await asyncio.to_thread(
            repo.search_by_type,
            user_id=user_id,
            query=query,
            entity_ids=entity_ids or None,
            analysis_type=analysis_type,
        )

        return format_memory_injection(memories)

    # ── 分析后：提取 + 摘要 + 整合检查 ─────────────────────

    def on_analysis_complete(
        self,
        result: Any,
        session_id: str,
        parsed_params: dict[str, Any],
    ) -> None:
        """分析完成后的记忆处理。全部 fire-and-forget，不阻塞响应。"""
        # 1. 结构化记忆提取
        if self._settings.memory.long_term_enabled:
            asyncio.create_task(
                self._extract_memories(result),
                name=f"mem_extract_{getattr(result, 'trace_id', '')}",
            )

        # 2. LLM 异步摘要（分析结果过长时触发）
        if self._settings.memory.short_term_summary_enabled:
            report_md = getattr(result, "report_markdown", "") or ""
            min_chars = self._settings.memory.short_term_summary_max_input_chars
            if len(report_md) > min_chars:
                asyncio.create_task(
                    self._summarize_result(report_md, session_id),
                    name=f"mem_summary_{session_id}",
                )

        # 3. 整合触发检查
        if self._settings.memory.consolidation.enabled:
            user_id = getattr(result, "user_id", "")
            if user_id:
                asyncio.create_task(
                    self._maybe_consolidate(user_id),
                    name=f"mem_consolidate_{user_id}",
                )

    # ── 每轮：反馈检测 ─────────────────────────────────────

    def on_user_message(
        self,
        query: str,
        session_id: str,
        session_context: dict[str, Any],
    ) -> None:
        """检测用户消息中的反馈/纠正信号。fire-and-forget。"""
        if not self._settings.memory.feedback.enabled:
            return

        detector = self._get_feedback_detector()
        signal = detector.detect_signal(query)  # 同步预筛，<1ms
        if signal:
            asyncio.create_task(
                self._extract_feedback(query, session_id, session_context, signal),
                name=f"mem_feedback_{session_id}",
            )

    # ── chat 消息索引（fire-and-forget）─────────────────────

    def on_chat_message(
        self,
        session_id: str,
        message_id: str,
        role: str,
        content: str,
        user_id: str,
    ) -> None:
        """每条 chat 消息写入后触发的索引钩子。fire-and-forget。"""
        if not self._settings.memory.chat_history.indexing_enabled:
            return
        indexer = self._get_chat_indexer()
        if indexer is not None:
            indexer.enqueue(
                session_id=session_id,
                message_id=message_id,
                role=role,
                content=content,
                user_id=user_id,
            )

    # ── SESSION_RECAP idle 触发 ──────────────────────────────

    async def on_session_idle(self, session_id: str, user_id: str) -> None:
        """会话不活跃超过阈值后触发摘要抽取。

        由 TaskRegistry idle watcher 调用。fire-and-forget 语义，
        所有异常内部捕获，不向调用方泄露。
        """
        if not self._settings.memory.session_recap.enabled:
            return
        try:
            summarizer = self._get_session_summarizer()
            await summarizer.extract(session_id=session_id, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "session recap extraction failed (non-blocking) session=%s: %s",
                session_id, exc,
            )

    # ── 跨会话历史对话检索 ──────────────────────────────────

    async def search_chat_history(
        self,
        user_id: str,
        query: str,
        entity_ids: list[str] | None = None,
        days: int | None = 30,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """跨会话历史对话检索（双通道：精确 + 语义 + 时间衰减）。

        同步执行，带超时保护。失败/超时返回空列表，不抛异常。
        """
        cfg = self._settings.memory.chat_history
        timeout = cfg.search_timeout_seconds
        try:
            return await asyncio.wait_for(
                self._search_chat_history_impl(
                    user_id, query, entity_ids, days, limit,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _logger.warning("chat history search timeout (%.1fs)", timeout)
            return []
        except Exception as exc:  # noqa: BLE001
            _logger.warning("chat history search failed: %s", exc)
            return []

    async def _search_chat_history_impl(
        self,
        user_id: str,
        query: str,
        entity_ids: list[str] | None,
        days: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """内部实现：双通道检索 + 时间衰减。"""
        from datetime import timedelta

        from core.time_utils import now_cn

        since = now_cn() - timedelta(days=days) if days else None
        seen_session_ids: set[str] = set()
        results: list[dict[str, Any]] = []

        # 通道 A（精确）：按 entity_ids 过滤 session_entities
        if entity_ids:
            exact_hits = await asyncio.to_thread(
                self._channel_a_exact, user_id, entity_ids, since, limit * 2,
            )
            for hit in exact_hits:
                if hit["session_id"] not in seen_session_ids:
                    hit["match_type"] = "exact"
                    hit["base_score"] = 1.0
                    results.append(hit)
                    seen_session_ids.add(hit["session_id"])

        # 通道 B（语义补充）：仅当 A 未填满
        if len(results) < limit:
            remaining = limit * 2 - len(results)
            semantic_hits = await asyncio.to_thread(
                self._channel_b_semantic, user_id, query, since, remaining,
            )
            for hit in semantic_hits:
                if hit["session_id"] not in seen_session_ids:
                    hit["match_type"] = "semantic"
                    results.append(hit)
                    seen_session_ids.add(hit["session_id"])

        # 时间衰减 rerank
        decay_cfg = self._settings.memory.recency_decay
        from core.memory.recency import apply_recency_decay

        apply_recency_decay(
            results,
            lambda_val=decay_cfg.lambda_val,
            enabled=decay_cfg.enabled,
        )

        return results[:limit]

    def _channel_a_exact(
        self,
        user_id: str,
        entity_ids: list[str],
        since: Any | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """通道 A：按 entity_ids 精确匹配 session_entities → 关联 chat_sessions。"""
        try:
            import sqlalchemy as sa

            from core.chat.tables import chat_sessions_table
            from core.database.engine import get_engine
            from core.memory.tables import session_entities_table

            engine = get_engine(self._settings.postgresql)
            j = chat_sessions_table.join(
                session_entities_table,
                chat_sessions_table.c.id == session_entities_table.c.session_id,
            )
            filters = [
                chat_sessions_table.c.user_id == user_id,
                chat_sessions_table.c.deleted_at.is_(None),
            ]
            if since is not None:
                filters.append(chat_sessions_table.c.updated_at >= since)

            entity_conditions = []
            for eid in entity_ids:
                entity_conditions.append(
                    sa.cast(session_entities_table.c.entities, sa.Text).like(f"%{eid}%")
                )
            if entity_conditions:
                filters.append(sa.or_(*entity_conditions))

            stmt = (
                sa.select(
                    chat_sessions_table.c.id.label("session_id"),
                    chat_sessions_table.c.title.label("session_title"),
                    chat_sessions_table.c.last_message_preview.label("snippet"),
                    session_entities_table.c.entities,
                    chat_sessions_table.c.updated_at.label("created_at"),
                )
                .select_from(j)
                .where(sa.and_(*filters))
                .order_by(chat_sessions_table.c.updated_at.desc())
                .limit(limit)
            )
            with engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
            return [
                {
                    "session_id": r.session_id,
                    "session_title": r.session_title or "",
                    "snippet": r.snippet or "",
                    "entities": r.entities or {},
                    "created_at": r.created_at,
                }
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001
            _logger.warning("channel_a_exact error: %s", exc)
            return []

    def _channel_b_semantic(
        self,
        user_id: str,
        query: str,
        since: Any | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """通道 B：Chroma chat_history 向量语义召回。"""
        indexer = self._get_chat_indexer()
        if indexer is None:
            return []

        store = indexer._get_vector_store()
        if store is None:
            return []

        where_filter: dict[str, Any] = {"user_id": user_id}
        try:
            hits = store.search(query=query, top_k=limit, where=where_filter)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("channel_b_semantic error: %s", exc)
            return []

        results: list[dict[str, Any]] = []
        for hit in hits:
            meta = hit.get("metadata", {})
            distance = hit.get("distance", 1.0)
            base_score = max(0.0, 1.0 - distance)
            results.append({
                "session_id": meta.get("session_id", ""),
                "session_title": "",
                "snippet": (hit.get("text", "") or "")[:200],
                "entities": {},
                "created_at": meta.get("created_at", ""),
                "base_score": base_score,
            })

        if since is not None:
            from datetime import datetime

            since_str = since.isoformat() if isinstance(since, datetime) else str(since)
            results = [r for r in results if str(r.get("created_at", "")) >= since_str]

        return results

    # ── 会话管理（委托 ShortTermMemory）─────────────────────

    def load_session(self, session_id: str) -> dict[str, Any]:
        """加载短期记忆上下文（对话历史 + 实体上下文）。"""
        return self._short_term.load_session_context(session_id)

    def save_session_entities(
        self, session_id: str, entities: dict[str, Any]
    ) -> None:
        """写入实体上下文到 session_entities 表。"""
        self._short_term.save_entity_context(session_id, entities)

    # ── Checkpointer 委托（保持 Orchestrator 向后兼容）──────

    def get_checkpointer(self) -> Any:
        """获取 LangGraph checkpointer。"""
        return self._short_term.get_checkpointer()

    def ensure_checkpointer(self) -> Any | None:
        """获取 checkpointer，失败返回 None。"""
        return self._short_term.ensure_checkpointer()

    def clear_short_term_memory(self, session_id: str | None = None) -> int:
        """清理短期记忆。"""
        return self._short_term.clear(session_id)

    async def save_dag_result(
        self,
        query: str,
        response: str,
        session_id: str,
        time_range_days: int,
        agent: Any,
    ) -> None:
        """将 DAG 执行的 query + response 写入 checkpointer 短期记忆。"""
        await self._short_term.save_dag_result(
            query, response, session_id, time_range_days, agent,
        )

    # ── 生命周期 ───────────────────────────────────────────

    def close(self) -> None:
        """关闭所有内部组件（进程退出时调用）。"""
        if self._chat_indexer is not None:
            self._chat_indexer.close()
        self._short_term.close()

    # ── 内部方法（private）──────────────────────────────────

    async def _maybe_consolidate(self, user_id: str) -> None:
        """检查是否触发整合，满足条件时执行。fire-and-forget。"""
        try:
            from core.database.engine import get_engine
            from core.memory.consolidation import run_consolidation, should_consolidate

            engine = get_engine(self._settings.postgresql)
            if should_consolidate(user_id, engine, self._settings):
                await run_consolidation(user_id, self._settings)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("consolidation check failed (non-blocking): %s", exc)

    async def _summarize_result(self, report_md: str, session_id: str) -> None:
        """异步 LLM 摘要压缩分析结果。fire-and-forget。"""
        try:
            from core.llm.model_factory import build_chat_model

            llm_fast = build_chat_model(self._settings, use_fast=True)
            await self._short_term.summarize_and_replace(
                session_id=session_id,
                ai_content=report_md,
                llm_fast=llm_fast,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("summary trigger failed (non-blocking): %s", exc)

    async def _extract_memories(self, result: Any) -> None:
        """异步记忆提取。fire-and-forget，所有异常内部捕获。"""
        try:
            from core.memory.extractor import MemoryExtractor
            from core.memory.long_term import get_memory_repository

            extractor = MemoryExtractor(
                repo=get_memory_repository(),
                settings=self._settings,
            )
            user_id = getattr(result, "user_id", "")
            session_id = getattr(result, "session_id", "")
            outcomes = await extractor.extract(
                result=result, user_id=user_id, session_id=session_id,
            )
            _logger.info("memory extraction completed: %s", outcomes)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("memory extraction failed (non-blocking): %s", exc)

    async def _extract_feedback(
        self,
        query: str,
        session_id: str,
        session_context: dict[str, Any],
        signal: str,
    ) -> None:
        """异步反馈提取。fire-and-forget，所有异常内部捕获。"""
        try:
            from core.llm.model_factory import build_chat_model
            from core.memory.long_term import get_memory_repository
            from core.memory.types import MemoryType

            detector = self._get_feedback_detector()
            llm_fast = build_chat_model(self._settings, use_fast=True)
            fb = await detector.extract(query, session_context, signal, llm_fast)

            if fb is None:
                return

            confidence = fb.get("confidence", 0.0)
            if confidence < self._settings.memory.feedback.min_confidence:
                return

            fb_type = fb["type"]
            type_map = {
                "correction": MemoryType.CORRECTION,
                "user_preference": MemoryType.USER_PREFERENCE,
                "domain_fact": MemoryType.DOMAIN_FACT,
            }
            memory_type = type_map.get(fb_type)
            if memory_type is None:
                return

            repo = get_memory_repository()
            entities = fb.get("related_entities", {})
            entity_id = (
                entities.get("vendor_id")
                or entities.get("po_number")
                or None
            )
            expires_at = repo.compute_expires_at(memory_type)

            repo.save(
                user_id=session_context.get("user_id", ""),
                session_id=session_id,
                memory_type=memory_type,
                content=fb["content"],
                metadata={
                    "source": "user_feedback",
                    "signal_type": signal,
                    "confidence": confidence,
                    "related_entities": entities,
                },
                entity_id=entity_id,
                expires_at=expires_at,
            )
            _logger.info(
                "feedback memory saved: type=%s confidence=%.2f",
                fb_type, confidence,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("feedback extraction failed (non-blocking): %s", exc)

    def _get_feedback_detector(self) -> Any:
        """懒初始化 FeedbackDetector。"""
        if self._feedback_detector is None:
            from core.memory.feedback import FeedbackDetector

            self._feedback_detector = FeedbackDetector()
        return self._feedback_detector

    def _get_chat_indexer(self) -> Any:
        """获取全局 ChatHistoryIndexer 单例。"""
        if self._chat_indexer is None:
            from core.memory import get_chat_indexer

            self._chat_indexer = get_chat_indexer()
        return self._chat_indexer

    def _get_session_summarizer(self) -> Any:
        """懒初始化 SessionSummaryExtractor。"""
        if self._session_summarizer is None:
            from core.memory.session_summary import SessionSummaryExtractor

            self._session_summarizer = SessionSummaryExtractor(self._settings)
        return self._session_summarizer
