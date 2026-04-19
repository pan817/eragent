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
            if k in ("supplier_id", "po_number", "invoice_number") and v
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
                entities.get("supplier_id")
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
