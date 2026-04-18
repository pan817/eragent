"""短期记忆：基于 LangGraph PostgresSaver 的 checkpointer 管理。

从 orchestrator.py 提取的 checkpointer 生命周期管理、会话上下文读取
和 DAG 结果写入逻辑。Orchestrator 持有 ShortTermMemory 实例并委托调用。
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from typing import Any

from core.logging_utils import get_logger

_logger = get_logger(__name__)


class ShortTermMemory:
    """Checkpointer 短期记忆管理。"""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._checkpointer: Any | None = None
        self._checkpointer_cm: Any | None = None
        self._lock = threading.RLock()

    # ── Checkpointer 生命周期 ─────────────────────────────────────

    def get_checkpointer(self) -> Any:
        """延迟构建 LangGraph PostgresSaver checkpointer。

        使用项目共用的 PostgreSQL，首次调用时进入 context manager
        并触发 setup() 建表。进程退出时通过 atexit 关闭底层连接。
        初始化失败时抛出异常，由调用方捕获降级。
        """
        if self._checkpointer is not None:
            return self._checkpointer
        with self._lock:
            if self._checkpointer is not None:
                return self._checkpointer
            from langgraph.checkpoint.postgres import PostgresSaver
            from core.observability.checkpointer import attach_tracing as attach_checkpointer_tracing

            conninfo = self._settings.postgresql.conninfo
            cm = PostgresSaver.from_conn_string(conninfo)
            saver = cm.__enter__()
            try:
                saver.setup()
            except Exception:
                cm.__exit__(None, None, None)
                raise
            # attach_tracing 需要 TimingMiddleware 实例，延迟获取
            try:
                from core.observability import TimingMiddleware
                attach_checkpointer_tracing(saver, TimingMiddleware(agent_name="p2p_agent"))
            except Exception as exc:
                _logger.info("checkpointer tracing attach skipped: %s", exc)
            self._checkpointer_cm = cm
            self._checkpointer = saver
            atexit.register(self.close)
            _logger.info("checkpointer initialized (ShortTermMemory)")
            return saver

    def close(self) -> None:
        """关闭 checkpointer 的 context manager（atexit 回调，幂等）。"""
        cm = self._checkpointer_cm
        if cm is None:
            return
        self._checkpointer_cm = None
        self._checkpointer = None
        try:
            cm.__exit__(None, None, None)
        except Exception as exc:
            _logger.info("checkpointer close failed (non-critical): %s", exc)

    def ensure_checkpointer(self) -> Any | None:
        """获取 checkpointer，初始化失败时返回 None（不阻塞主流程）。"""
        try:
            return self.get_checkpointer()
        except Exception as exc:
            _logger.warning(
                "checkpointer init failed, short-term memory disabled: %s", exc
            )
            from core.observability.tracing import record_span

            with record_span(
                "checkpoint", "checkpointer_init_failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            ) as span_attrs:
                span_attrs["status"] = "error"
            return None

    # ── 公共操作 ──────────────────────────────────────────────────

    def clear(self, session_id: str | None = None) -> int:
        """清理短期记忆（checkpointer 中对应 thread_id 的历史）。"""
        if self._checkpointer is None:
            return 0
        if session_id is None:
            return 0
        try:
            self._checkpointer.delete_thread(session_id)
            return 1
        except Exception as exc:
            _logger.warning("clear short-term memory failed: %s", exc)
            return 0

    def load_session_context(self, session_id: str) -> dict[str, Any]:
        """从 checkpointer 读取当前 session 的对话历史，提取上下文信息。

        返回字典包含：
        - context_summary: 最近一轮 AI 回复的摘要
        - entities: 从历史中提取的实体（po_number / supplier_id）
        - has_history: 是否有历史对话

        读取失败返回空上下文，不阻塞主流程。
        """
        from core.observability.tracing import record_span
        from core.orchestrator.router import _extract_params

        empty: dict[str, Any] = {"has_history": False, "context_summary": "", "entities": {}}

        with record_span("checkpoint", "load_session_context") as span_attrs:
            span_attrs["session_id"] = session_id

            try:
                checkpointer = self.ensure_checkpointer()
                if checkpointer is None:
                    span_attrs["status"] = "skipped"
                    span_attrs["reason"] = "checkpointer not available"
                    return empty

                config = {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}
                existing = checkpointer.get_tuple(config)

                if not existing or not existing.checkpoint:
                    span_attrs["status"] = "ok"
                    span_attrs["has_history"] = False
                    return empty

                channel_values = existing.checkpoint.get("channel_values", {})
                messages = channel_values.get("messages", [])
                if not messages:
                    span_attrs["status"] = "ok"
                    span_attrs["has_history"] = False
                    return empty

                # 提取最近一轮的 HumanMessage + AIMessage
                last_human = ""
                last_ai = ""
                for msg in reversed(messages):
                    msg_type = getattr(msg, "type", "")
                    content = getattr(msg, "content", "")
                    if msg_type == "ai" and not last_ai:
                        last_ai = content
                    elif msg_type == "human" and not last_human:
                        last_human = content
                    if last_human and last_ai:
                        break

                # 从历史 query 和 response 中提取实体
                entities: dict[str, Any] = {}
                for text in [last_human, last_ai]:
                    extracted = _extract_params(
                        text, self._settings.analysis.entity_patterns
                    )
                    for key, val in extracted.items():
                        if key not in entities and val:
                            entities[key] = val

                # 集中裁剪：所有路径的短期记忆都经过此产出点
                context_summary = last_ai if last_ai else ""
                if context_summary and self._settings.memory.short_term_context_trim_enabled:
                    from modules.p2p.prompts import trim_to_token_budget
                    max_tokens = int(
                        self._settings.llm.context_window
                        * self._settings.memory.short_term_context_max_tokens_pct
                        / 100
                    )
                    context_summary = trim_to_token_budget(
                        context_summary, max_tokens, "短期记忆"
                    )

                span_attrs["status"] = "ok"
                span_attrs["has_history"] = True
                span_attrs["n_messages"] = len(messages)
                span_attrs["entities_found"] = list(entities.keys())

                return {
                    "has_history": True,
                    "context_summary": context_summary,
                    "entities": entities,
                }

            except Exception as exc:
                span_attrs["status"] = "error"
                span_attrs["error"] = str(exc)
                _logger.warning("load session context failed (non-blocking): %s", exc)
                return empty

    async def save_dag_result(
        self,
        query: str,
        response: str,
        session_id: str,
        time_range_days: int,
        agent: Any,
    ) -> None:
        """将 DAG 执行的 query + response 写入 checkpointer 短期记忆。

        通过 agent.update_state 写入，确保 checkpoint 的 channel 格式
        与 LangGraph agent 内部一致。写入失败不阻塞主流程。
        """
        from core.observability.tracing import record_span

        with record_span("checkpoint", "dag_short_term_write") as span_attrs:
            span_attrs["session_id"] = session_id
            span_attrs["query_length"] = len(query)
            span_attrs["response_length"] = len(response)

            try:
                # 写入前截断历史，防止消息无限累积
                max_short_term = self._settings.memory.short_term_max_messages
                agent._truncate_checkpointer_history(session_id, max_short_term)

                agent_graph = agent._get_or_build_agent()
                if agent_graph is None:
                    span_attrs["status"] = "skipped"
                    span_attrs["reason"] = "agent not available"
                    return

                from langchain_core.messages import AIMessage, HumanMessage

                user_msg = HumanMessage(
                    content=f"{query}\n\n[分析参数] 时间范围: 最近 {time_range_days} 天"
                )
                ai_msg = AIMessage(content=response or "(DAG 分析完成，报告为空)")

                config = {"configurable": {"thread_id": session_id}}

                await asyncio.to_thread(
                    agent_graph.update_state,
                    config,
                    {"messages": [user_msg, ai_msg]},
                )

                span_attrs["status"] = "ok"
                span_attrs["n_messages"] = 2
                _logger.info(
                    "DAG result saved to short-term memory: session=%s", session_id
                )

            except Exception as exc:
                span_attrs["status"] = "error"
                span_attrs["error"] = str(exc)
                _logger.warning(
                    "DAG short-term memory write failed (non-blocking): %s", exc
                )
