"""
P2P Agent 定义。

基于 LangChain 1.2.0 create_agent API 构建 P2P 采购分析智能体，
使用 ChatOpenAI 兼容接口连接 GLM-4 大模型，集成 8 个结构化工具
实现采购订单查询、三路匹配、价格差异分析、付款合规检查和供应商绩效计算。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from langchain.agents import create_agent

from api.schemas.analysis import (
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    ErrorInfo,
)
from config.settings import Settings, get_settings
from core.logging_utils import get_logger
from core.observability import TimingMiddleware
from core.observability.middleware import estimate_tokens, record_span
from modules.p2p.model_factory import build_chat_model
from modules.p2p.prompts import build_system_prompt, format_long_term_memory

_logger = get_logger(__name__)

# 从 anomalies 中聚合的实体类型上限（供应商/PO 号各最多保留这么多个，避免 metadata 过大）
_MAX_ENTITIES_PER_TYPE = 10
# 写入长期记忆的 response 截断长度
_MEMORY_RESPONSE_MAX_LEN = 1500


def _build_memory_content(
    query: str,
    response: str,
    summary: dict[str, Any],
) -> str:
    """构建写入长期记忆的 content 字段。

    格式：Q / Summary（若有）/ A 三段，使得 FTS 与向量召回都能命中
    关键信息。
    """
    parts: list[str] = [f"Q: {query}"]
    if summary:
        # 取 summary 中几个有代表性的字段拼到 content 里，增强 FTS 命中率
        anomaly_count = summary.get("anomaly_count") or summary.get("total_anomalies")
        if anomaly_count is not None:
            parts.append(f"异常数量: {anomaly_count}")
        text_summary = summary.get("summary") or summary.get("description")
        if text_summary:
            parts.append(f"摘要: {str(text_summary)[:300]}")
    parts.append(f"A: {response[:_MEMORY_RESPONSE_MAX_LEN]}")
    return "\n".join(parts)


def _build_memory_metadata(
    query: str,
    analysis_type: str,
    anomalies: list[dict[str, Any]],
    summary: dict[str, Any],
    time_range_days: int,
) -> dict[str, Any]:
    """从本轮分析结果中提取结构化字段，作为 memory metadata 写入。

    包含：
    - ``analysis_type``：分析类型（用于后续按类型检索/去重）
    - ``anomaly_count``：本轮检出异常数（用于 L3 过滤判断）
    - ``time_range_days``：分析时间范围
    - ``summary``：文字摘要（从 summary dict 中取出）
    - ``entities``：从 anomalies 中聚合的关键实体
        - ``suppliers``：涉及的供应商列表（去重，最多 _MAX_ENTITIES_PER_TYPE 个）
        - ``po_numbers``：涉及的 PO 号列表（去重，最多 _MAX_ENTITIES_PER_TYPE 个）
    """
    # 从 anomalies 中提取实体
    suppliers: list[str] = []
    po_numbers: list[str] = []
    seen_sup: set[str] = set()
    seen_po: set[str] = set()
    for anomaly in anomalies or []:
        docs = anomaly.get("documents") or {}
        sup = docs.get("supplier_name") or ""
        po = docs.get("po_number") or ""
        if sup and sup not in seen_sup and len(suppliers) < _MAX_ENTITIES_PER_TYPE:
            suppliers.append(sup)
            seen_sup.add(sup)
        if po and po not in seen_po and len(po_numbers) < _MAX_ENTITIES_PER_TYPE:
            po_numbers.append(po)
            seen_po.add(po)

    text_summary = ""
    if summary:
        text_summary = str(
            summary.get("summary") or summary.get("description") or ""
        )[:500]

    return {
        "query": query,
        "analysis_type": analysis_type,
        "anomaly_count": len(anomalies) if anomalies else 0,
        "time_range_days": time_range_days,
        "summary": text_summary,
        "entities": {
            "suppliers": suppliers,
            "po_numbers": po_numbers,
        },
    }


class P2PAgent:
    """P2P 采购分析智能体。

    封装 LangChain Agent，提供自然语言驱动的采购到付款流程分析能力。
    支持三路匹配异常检测、价格差异分析、付款合规检查和供应商绩效评估。

    Agent 和 LLM 均采用延迟加载策略，在首次调用 analyze() 时才初始化，
    避免在导入或实例化阶段产生不必要的网络请求。

    Attributes:
        _settings: 全局配置对象。
        _agent: LangChain Agent 实例（延迟初始化）。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        timing_middleware: TimingMiddleware | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        """初始化 P2P Agent。

        Args:
            settings: 全局配置对象，为 None 时自动调用 get_settings() 获取。
            timing_middleware: 链路监控中间件。允许调用方（如 Orchestrator）
                在更外层创建并复用同一份 middleware，确保 trace 能覆盖
                P2PAgent 的导入与构建阶段。为 None 时内部懒建一份。
            checkpointer: 外部注入的 LangGraph PostgresSaver 实例，
                由 Orchestrator 统一管理生命周期。为 None 时 Agent
                以无短期记忆模式运行。
        """
        self._settings: Settings = settings if settings is not None else get_settings()
        self._agent: Any | None = None
        self._timing_middleware: TimingMiddleware | None = timing_middleware
        self._checkpointer: Any | None = checkpointer
        self._lock = threading.RLock()

    @property
    def timing_middleware(self) -> TimingMiddleware:
        """获取 TimingMiddleware 实例（懒初始化）。

        在 Agent 真正构建前，Orchestrator 也可以拿到 middleware 用于
        包裹整条调用链 —— 这样首次构建 Agent 的耗时（LLM client、
        本体加载、create_agent 组装）也能落入 trace。
        """
        if self._timing_middleware is None:
            self._timing_middleware = TimingMiddleware(agent_name="p2p_agent")
        return self._timing_middleware

    # ------------------------------------------------------------------
    # 构建方法
    # ------------------------------------------------------------------

    def _truncate_checkpointer_history(self, thread_id: str, max_messages: int) -> None:
        """截断 checkpointer 中的历史消息，防止超出 LLM 上下文限制。

        保留最近 max_messages 条消息，裁剪旧消息后写回。
        失败时静默跳过，不阻塞主流程。
        """
        if self._checkpointer is None or max_messages <= 0:
            return

        try:
            config = {"configurable": {"thread_id": thread_id}}
            existing = self._checkpointer.get_tuple(config)
            if not existing or not existing.checkpoint:
                return

            channel_values = existing.checkpoint.get("channel_values", {})
            messages = channel_values.get("messages", [])

            if len(messages) <= max_messages:
                return  # 未超限，无需截断

            _logger.info(
                "truncating short-term memory: %d → %d messages (thread=%s)",
                len(messages),
                max_messages,
                thread_id,
            )

            # 保留最近 N 条
            channel_values["messages"] = messages[-max_messages:]
            existing.checkpoint["channel_values"] = channel_values
            existing.checkpoint["id"] = str(uuid.uuid4())

            metadata = {
                "source": "truncate",
                "step": (existing.metadata or {}).get("step", 0) + 1,
                "writes": None,
            }
            self._checkpointer.put(config, existing.checkpoint, metadata, {})

        except Exception as exc:  # noqa: BLE001
            _logger.warning("checkpointer truncation failed (non-blocking): %s", exc)

    def _estimate_tool_definitions_tokens(self) -> int:
        """估算工具定义（function schema）的 token 数量。

        LangChain 在每次 LLM 调用时都会携带所有工具的 JSON Schema，
        包含 name、description、parameters。此方法计算一次后缓存。
        """
        if hasattr(self, "_tool_definitions_tokens_cache"):
            return self._tool_definitions_tokens_cache

        try:
            tools = self._build_tools()
            total_chars = 0
            for t in tools:
                total_chars += len(getattr(t, "name", ""))
                total_chars += len(getattr(t, "description", "") or "")
                schema = getattr(t, "args_schema", None)
                if schema:
                    schema_dict = schema.schema() if callable(getattr(schema, "schema", None)) else {}
                    total_chars += len(json.dumps(schema_dict, ensure_ascii=False))
            result = estimate_tokens("x" * total_chars)
        except Exception:
            result = 0
        self._tool_definitions_tokens_cache = result
        return result

    def _estimate_checkpointer_tokens(self, thread_id: str) -> tuple[int, int]:
        """估算 checkpointer 中历史消息的 token 数量。

        Returns:
            (token 数, 消息条数) 元组。checkpointer 不可用时返回 (0, 0)。
        """
        if self._checkpointer is None:
            return 0, 0
        try:
            config = {"configurable": {"thread_id": thread_id}}
            existing = self._checkpointer.get_tuple(config)
            if not existing or not existing.checkpoint:
                return 0, 0
            messages = existing.checkpoint.get("channel_values", {}).get("messages", [])
            total_chars = 0
            for msg in messages:
                content = getattr(msg, "content", "")
                if isinstance(content, str):
                    total_chars += len(content)
            return estimate_tokens("x" * total_chars), len(messages)
        except Exception:
            return 0, 0

    def _record_context_budget(
        self,
        *,
        context_summary: str,
        long_term_text: str,
        long_term_count: int,
        user_message: str,
        tool_definitions_tokens: int,
        checkpointer_history_tokens: int,
        checkpointer_message_count: int,
    ) -> None:
        """记录 context_budget span，追踪注入 LLM 的各部分 token 占比。"""
        system_prompt = build_system_prompt()
        # 从系统提示词中分离出本体上下文部分的 token
        from modules.p2p.prompts import get_ontology_context
        ontology_text = get_ontology_context()
        ontology_tokens = estimate_tokens(ontology_text)
        system_prompt_tokens = estimate_tokens(system_prompt) - ontology_tokens

        short_term_tokens = estimate_tokens(context_summary)
        long_term_tokens = estimate_tokens(long_term_text)
        user_message_tokens = estimate_tokens(user_message)

        total_inject_tokens = (
            system_prompt_tokens
            + ontology_tokens
            + long_term_tokens
            + short_term_tokens
            + user_message_tokens
            + tool_definitions_tokens
            + checkpointer_history_tokens
        )
        context_window = self._settings.llm.context_window
        budget_usage_pct = round(
            total_inject_tokens / context_window * 100, 1
        ) if context_window > 0 else 0.0

        with record_span("context_budget", "context_budget",
                         system_prompt_tokens=system_prompt_tokens,
                         ontology_context_tokens=ontology_tokens,
                         long_term_memory_tokens=long_term_tokens,
                         long_term_memory_count=long_term_count,
                         short_term_memory_tokens=short_term_tokens,
                         user_message_tokens=user_message_tokens,
                         tool_definitions_tokens=tool_definitions_tokens,
                         checkpointer_history_tokens=checkpointer_history_tokens,
                         checkpointer_message_count=checkpointer_message_count,
                         total_inject_tokens=total_inject_tokens,
                         model_context_limit=context_window,
                         budget_usage_pct=budget_usage_pct):
            pass  # 纯记录，无业务逻辑

    def _build_tools(self) -> list:
        """导入并返回 P2P 工具集。

        从 modules.p2p.tools 模块导入全部 8 个 @tool 装饰的工具函数。

        Returns:
            包含 8 个 LangChain Tool 对象的列表。
        """
        from modules.p2p.tools import (
            calculate_supplier_kpis,
            query_invoices,
            query_payments,
            query_purchase_orders,
            query_receipts,
            run_payment_compliance_check,
            run_price_variance_analysis,
            run_three_way_match,
        )

        return [
            query_purchase_orders,
            query_receipts,
            query_invoices,
            query_payments,
            run_three_way_match,
            run_price_variance_analysis,
            run_payment_compliance_check,
            calculate_supplier_kpis,
        ]

    def _get_or_build_agent(self) -> Any:
        """获取或延迟构建 LangChain Agent。

        首次调用时初始化 LLM 模型、工具集和系统提示词，
        使用 create_agent 组装完整的 Agent 实例并缓存。

        Returns:
            LangChain Agent 实例。
        """
        if self._agent is not None:
            return self._agent
        with self._lock:
            if self._agent is not None:
                return self._agent
            model = build_chat_model(self._settings.llm)
            tools = self._build_tools()
            system_prompt = build_system_prompt()
            middleware = self.timing_middleware
            agent_kwargs: dict[str, Any] = dict(
                model=model,
                tools=tools,
                system_prompt=system_prompt,
                name="p2p_agent",
                middleware=[middleware],
            )
            if self._checkpointer is not None:
                agent_kwargs["checkpointer"] = self._checkpointer
            self._agent = create_agent(**agent_kwargs)
            return self._agent

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    async def run(
        self,
        analysis_type: AnalysisType,
        query: str,
        params: dict[str, Any] | None = None,
        time_range_days: int = 30,
        user_id: str = "default",
        session_id: str = "",
        context_summary: str = "",
        skip_memory_write: bool = False,
        output_mode_prompt: str = "",
    ) -> dict[str, Any]:
        """供 Orchestrator 调用的异步入口。

        Args:
            analysis_type: 分析类型。
            query: 自然语言查询。
            params: 意图解析提取的额外参数（supplier_id, po_number 等）。
            time_range_days: 分析时间范围（天）。
            user_id: 用户 ID。
            session_id: 会话 ID。
            context_summary: 上一轮对话的 AI 回复摘要（来自 checkpointer）。
            skip_memory_write: 跳过长期记忆写入（非分析意图时避免循环引用）。
            output_mode_prompt: 输出模式格式指令。

        Returns:
            包含 anomalies, supplier_kpis, summary, report_markdown,
            completed_tasks, failed_tasks 的字典。
        """
        result = await self.analyze(
            query=query,
            user_id=user_id,
            session_id=session_id,
            time_range_days=time_range_days,
            context_summary=context_summary,
            skip_memory_write=skip_memory_write,
            output_mode_prompt=output_mode_prompt,
        )

        return {
            "anomalies": [a.model_dump(mode="json") for a in result.anomalies],
            "supplier_kpis": [k.model_dump(mode="json") for k in result.supplier_kpis],
            "summary": result.summary,
            "report_markdown": result.report_markdown,
            "completed_tasks": result.completed_tasks,
            "failed_tasks": result.failed_tasks,
        }

    async def analyze(
        self,
        query: str,
        user_id: str = "default",
        session_id: str = "",
        time_range_days: int = 30,
        context_summary: str = "",
        skip_memory_write: bool = False,
        output_mode_prompt: str = "",
    ) -> AnalysisResult:
        """执行 P2P 分析任务。

        接收自然语言查询，驱动 Agent 调用工具链完成分析，
        返回结构化的 AnalysisResult。支持自动重试机制。

        Args:
            query: 自然语言分析查询，如 "检查供应商 SUP-001 的三路匹配情况"。
            user_id: 用户 ID，默认 "default"。
            session_id: 会话 ID，为空则自动生成。
            time_range_days: 分析时间范围（天），默认 30 天。

        Returns:
            AnalysisResult 实例，包含分析状态、异常列表、Markdown 报告等。
        """
        report_id: str = str(uuid.uuid4())
        if not session_id:
            session_id = str(uuid.uuid4())

        start_time: float = time.monotonic()
        max_retries: int = self._settings.llm.max_retries
        last_error: Exception | None = None

        # ── 构建本轮消息（重试循环外部，避免重复注入系统消息）──
        output_hint = f"\n[输出格式] {output_mode_prompt}" if output_mode_prompt else ""
        user_message: str = (
            f"{query}\n\n"
            f"[分析参数] 时间范围: 最近 {time_range_days} 天"
            f"{output_hint}"
        )

        invoke_messages: list[tuple[str, str]] = []

        # 注入上一轮对话摘要
        if context_summary:
            invoke_messages.append((
                "system",
                f"[对话历史-上一轮分析结果摘要]\n{context_summary}",
            ))

        # 注入长期记忆
        long_term_snippets: list[dict[str, Any]] = []
        if self._settings.memory.long_term_enabled and not skip_memory_write:
            try:
                from core.memory import get_long_term_memory

                ltm = get_long_term_memory()
                max_retrieved = self._settings.memory.long_term_max_retrieved
                long_term_snippets = ltm.search_memories(
                    user_id=user_id, query=query, limit=max_retrieved
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("long-term memory retrieval skipped: %s", exc)

        long_term_text = format_long_term_memory(long_term_snippets)
        if long_term_text:
            invoke_messages.insert(
                0,
                (
                    "system",
                    f"[长期记忆-历史参考]\n以下是该用户与本次查询相关的历史记忆,"
                    f"可作为分析参考:\n{long_term_text}",
                ),
            )

        invoke_messages.append(("user", user_message))

        effective_thread = (
            f"_recall_{uuid.uuid4().hex[:8]}"
            if skip_memory_write
            else session_id
        )
        invoke_config: dict[str, Any] = {
            "configurable": {"thread_id": effective_thread}
        }

        # ── 短期记忆截断：防止历史消息无限累积超出 LLM 上下文限制 ──
        max_short_term = self._settings.memory.short_term_max_messages
        self._truncate_checkpointer_history(effective_thread, max_short_term)

        # ── 记录 context_budget span：各部分 token 占比 ──
        tool_def_tokens = self._estimate_tool_definitions_tokens()
        cp_tokens, cp_count = self._estimate_checkpointer_tokens(effective_thread)
        self._record_context_budget(
            context_summary=context_summary,
            long_term_text=long_term_text,
            long_term_count=len(long_term_snippets),
            user_message=user_message,
            tool_definitions_tokens=tool_def_tokens,
            checkpointer_history_tokens=cp_tokens,
            checkpointer_message_count=cp_count,
        )

        # ── 重试循环 ──
        for attempt in range(max_retries):
            try:
                agent = self._get_or_build_agent()
                result: dict[str, Any] = await agent.ainvoke(
                    {"messages": invoke_messages},
                    config=invoke_config,
                )

                # 提取最终回复
                messages: list[Any] = result.get("messages", [])
                content: str = ""
                if messages:
                    last_message = messages[-1]
                    content = (
                        last_message.content
                        if hasattr(last_message, "content")
                        else str(last_message)
                    )

                # 短期记忆由 checkpointer 自动写回,无需手动 append。

                # 尝试从 content 中解析结构化 JSON
                anomalies: list[dict[str, Any]] = []
                summary: dict[str, Any] = {}
                analysis_type: AnalysisType = AnalysisType.COMPREHENSIVE

                try:
                    parsed: Any = json.loads(content)
                    if isinstance(parsed, dict):
                        anomalies = parsed.get("anomalies", [])
                        summary = parsed.get("summary", {})
                        if "analysis_type" in parsed:
                            analysis_type = AnalysisType(parsed["analysis_type"])
                except (json.JSONDecodeError, ValueError):
                    pass  # content 是纯文本 Markdown 报告，无需解析

                # 写回长期记忆：解析完结构化字段后写入，携带完整 metadata。
                # skip_memory_write=True 时跳过（非分析意图，避免循环引用）。
                # 失败不阻塞主路径。
                if content and self._settings.memory.long_term_enabled and not skip_memory_write:
                    try:
                        from core.memory import get_long_term_memory

                        ltm = get_long_term_memory()
                        ltm.save_memory(
                            user_id=user_id,
                            session_id=session_id,
                            memory_type="analysis_conclusion",
                            content=_build_memory_content(
                                query=query,
                                response=content,
                                summary=summary,
                            ),
                            metadata=_build_memory_metadata(
                                query=query,
                                analysis_type=analysis_type.value,
                                anomalies=anomalies,
                                summary=summary,
                                time_range_days=time_range_days,
                            ),
                        )
                    except Exception as exc:  # noqa: BLE001
                        _logger.warning("long-term memory save skipped: %s", exc)

                elapsed_ms: float = (time.monotonic() - start_time) * 1000

                return AnalysisResult(
                    report_id=report_id,
                    status=AnalysisStatus.SUCCESS,
                    analysis_type=analysis_type,
                    query=query,
                    user_id=user_id,
                    session_id=session_id,
                    time_range=f"最近 {time_range_days} 天",
                    report_markdown=content,
                    summary=summary,
                    duration_ms=round(elapsed_ms, 2),
                    created_at=datetime.utcnow(),
                )

            except Exception as e:
                last_error = e
                _logger.warning(
                    "p2p agent invoke failed (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    e,
                )
                # 指数退避：base * 2**attempt，封顶 max。最后一次不再 sleep
                if attempt < max_retries - 1:
                    runtime_cfg = self._settings.agent_runtime
                    backoff = min(
                        runtime_cfg.retry_backoff_base_seconds * (2 ** attempt),
                        runtime_cfg.retry_backoff_max_seconds,
                    )
                    await asyncio.sleep(backoff)
                # 不重置 self._agent：LLM client、本体、工具集均可复用，
                # 重建一次代价高达数秒。
                continue

        # 所有重试均失败
        elapsed_ms = (time.monotonic() - start_time) * 1000
        error_msg: str = str(last_error) if last_error else "未知错误"

        return AnalysisResult(
            report_id=report_id,
            status=AnalysisStatus.FAILED,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query=query,
            user_id=user_id,
            session_id=session_id,
            time_range=f"最近 {time_range_days} 天",
            report_markdown="",
            error=ErrorInfo(
                code="AGENT_INVOKE_FAILED",
                message=f"P2P 分析失败: {error_msg}",
                retry_count=max_retries,
            ),
            duration_ms=round(elapsed_ms, 2),
            created_at=datetime.utcnow(),
        )
