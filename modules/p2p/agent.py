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

from api.schemas.domain import (
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    ErrorInfo,
)
from config.settings import Settings, get_settings
from core.logging_utils import get_logger
from core.observability import TimingMiddleware
from core.observability.middleware import estimate_tokens, record_span
from core.time_utils import now_cn
from core.llm.model_factory import build_chat_model
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
        except Exception as exc:
            _logger.warning("_estimate_tool_definitions_tokens failed: %s", exc)
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
        except Exception as exc:
            _logger.warning("_estimate_checkpointer_tokens failed: %s", exc)
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

            from core.memory.trimmer import MemoryMiddleware

            mem_cfg = self._settings.memory
            memory_middleware = MemoryMiddleware(
                enabled=mem_cfg.react_trim_enabled,
                keep_recent_rounds=mem_cfg.react_keep_recent_rounds,
                tool_content_max_chars=mem_cfg.react_tool_content_max_chars,
            )
            # MemoryMiddleware 在前（外层裁剪），TimingMiddleware 在后（内层记录裁剪后 token）
            agent_kwargs: dict[str, Any] = dict(
                model=model,
                tools=tools,
                system_prompt=system_prompt,
                name="p2p_agent",
                middleware=[memory_middleware, self.timing_middleware],
            )
            if self._checkpointer is not None:
                agent_kwargs["checkpointer"] = self._checkpointer
            self._agent = create_agent(**agent_kwargs)
            return self._agent

    # ------------------------------------------------------------------
    # 流式工具：ReAct 兜底路径的 token-level 推送
    # ------------------------------------------------------------------

    # micro-batch 参数：每累计 _STREAM_FLUSH_CHARS 字符或每 _STREAM_FLUSH_INTERVAL
    # 秒（先到者为准）往 EventBus flush 一次。与 Phase 1 ReportAgent 一致，
    # 把 token 级事件聚合成 ~20 Hz 的 chunk，兼顾体验与带宽。
    _STREAM_FLUSH_CHARS: int = 16
    _STREAM_FLUSH_INTERVAL: float = 0.05

    async def _astream_react_with_publish(
        self,
        agent: Any,
        invoke_input: dict[str, Any],
        invoke_config: dict[str, Any],
        *,
        trace_id: str,
        message_id: str,
        node: str = "agent_final",
        span_attrs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """用 ``agent.astream_events(version="v2")`` 替代 ``ainvoke``，
        仅对最终 text turn 做 token-level 流式推送（first-chunk 模式检测）。

        每个 LLM turn 只会输出 ``content`` 或 ``tool_call_chunks`` 之一。
        我们用首个非空 chunk 判定 turn 模式：

        - ``tool_call_chunks`` 非空 → tool turn，丢弃整轮（不推送）
        - ``content`` 非空 → text turn，按 micro-batch 实时推送
        - 都为空 → 继续等下一个 chunk（计入 ``ambiguous_chunks`` 指标）

        混输 rollback：text turn 推送途中又冒出 ``tool_call_chunks``，
        说明模型在文本中途切换到工具调用。此时已推 chunk 是污染数据，
        发一帧 ``index=0, delta=""`` 重置帧让前端清 buffer，并把当前 turn
        重新当 tool turn 处理。

        Returns:
            与 ``agent.ainvoke`` 等价的 ``dict``：包含 ``messages`` 列表，
            供调用方提取最终回复 content 与 JSON 兜底解析。
        """
        from core.observability.middleware import _current_trace
        from core.tasks.events import get_event_bus
        from core.tasks.stream_utils import (
            ThinkTagFilter,
            extract_chunk_text,
            publish_chunk_event,
            strip_think_tags,
        )

        bus = get_event_bus()
        attrs: dict[str, Any] = span_attrs if span_attrs is not None else {}

        # 流式状态
        accumulated_raw: list[str] = []  # 累加原始文本（含 <think>）
        pending: list[str] = []          # 待 flush 的可见文本
        chunk_index = 0
        last_flush_ts = time.monotonic()
        first_chunk_ms: float | None = None
        stream_start_monotonic = time.monotonic()

        # turn 级状态
        current_turn_mode: str | None = None  # None | "text" | "tool"
        text_turns = 0
        tool_turns = 0
        ambiguous_chunks = 0
        rollback_triggered = False

        think_filter = ThinkTagFilter()

        # 最终消息（从 LangGraph 顶层 on_chain_end 拿）
        final_messages: list[Any] = []
        final_meta: Any = None

        def _flush(eos: bool = False) -> None:
            nonlocal chunk_index, last_flush_ts
            if not pending and not eos:
                return
            delta = "".join(pending)
            pending.clear()
            publish_chunk_event(
                bus,
                trace_id=trace_id,
                node=node,
                message_id=message_id,
                delta=delta,
                index=chunk_index,
                eos=eos,
            )
            chunk_index += 1
            last_flush_ts = time.monotonic()

        def _publish_rollback() -> None:
            """混输场景：发 index=0、delta="" 的重置帧让前端清 buffer。

            绕过 ``publish_chunk_event`` 的"空 delta + 非 eos 跳过"优化——
            rollback 帧就是要空 delta 表达"清 buffer"语义。
            """
            from core.time_utils import now_cn

            nonlocal chunk_index, rollback_triggered
            if bus is not None:
                bus.publish(
                    trace_id,
                    {
                        "type": "chunk",
                        "trace_id": trace_id,
                        "ts": now_cn().isoformat(),
                        "seq": 0,
                        "node": node,
                        "message_id": message_id,
                        "delta": "",
                        "index": 0,
                        "eos": False,
                    },
                    ephemeral=True,
                )
            pending.clear()
            accumulated_raw.clear()
            think_filter.__init__()  # 重置过滤器状态，下一轮 text turn 干净开始
            chunk_index = 0
            rollback_triggered = True

        async for event in agent.astream_events(
            invoke_input, config=invoke_config, version="v2"
        ):
            ev_type = event.get("event", "")
            data = event.get("data") or {}

            if ev_type == "on_chat_model_start":
                # 新一轮 LLM turn，重置 turn 模式
                current_turn_mode = None

            elif ev_type == "on_chat_model_stream":
                chunk = data.get("chunk")
                if chunk is None:
                    continue

                content_text = extract_chunk_text(chunk)
                tool_chunks = getattr(chunk, "tool_call_chunks", None)
                meta = getattr(chunk, "usage_metadata", None)
                if meta:
                    final_meta = meta

                # 模式判定
                if current_turn_mode is None:
                    if tool_chunks:
                        current_turn_mode = "tool"
                    elif content_text:
                        current_turn_mode = "text"
                        text_turns += 1  # 进入新的 text turn
                    else:
                        # 既无 content 也无 tool_call_chunks，继续等
                        ambiguous_chunks += 1
                        continue

                # text turn 中途冒出 tool_call_chunks → 混输 rollback
                if current_turn_mode == "text" and tool_chunks:
                    _publish_rollback()
                    current_turn_mode = "tool"
                    # text_turns 已经加过 1，回滚此次计数
                    text_turns = max(0, text_turns - 1)
                    continue

                if current_turn_mode == "text" and content_text:
                    # 经 think-tag filter 处理
                    visible, raw = think_filter.feed(content_text)
                    if raw:
                        accumulated_raw.append(raw)
                    if visible:
                        pending.append(visible)
                        if first_chunk_ms is None:
                            first_chunk_ms = (
                                time.monotonic() - stream_start_monotonic
                            ) * 1000.0
                    if (
                        sum(len(s) for s in pending) >= self._STREAM_FLUSH_CHARS
                        or (time.monotonic() - last_flush_ts)
                        >= self._STREAM_FLUSH_INTERVAL
                    ):
                        _flush()

            elif ev_type == "on_chat_model_end":
                if current_turn_mode == "text":
                    # text turn 正常结束：flush 边界缓冲 + eos
                    tail_visible, tail_raw = think_filter.flush()
                    if tail_raw:
                        accumulated_raw.append(tail_raw)
                    if tail_visible:
                        pending.append(tail_visible)
                    _flush(eos=True)
                elif current_turn_mode == "tool":
                    tool_turns += 1
                # 模式归零，等待下一轮 turn

            elif ev_type == "on_chain_end":
                # 顶层 LangGraph 图执行结束，拿 final state
                if event.get("name") in ("LangGraph", "agent", "p2p_agent"):
                    output = data.get("output") or {}
                    if isinstance(output, dict):
                        msgs = output.get("messages") or []
                        if msgs:
                            final_messages = list(msgs)

        # 监控指标回填
        attrs["text_turns"] = text_turns
        attrs["tool_turns"] = tool_turns
        attrs["ambiguous_chunks"] = ambiguous_chunks
        attrs["rollback_triggered"] = rollback_triggered
        if first_chunk_ms is not None:
            attrs["first_chunk_ms"] = round(first_chunk_ms, 2)

        # 兜底：final_messages 为空时用 accumulated 重构 AIMessage
        if not final_messages and accumulated_raw:
            try:
                from langchain_core.messages import AIMessage

                final_text = strip_think_tags("".join(accumulated_raw))
                final_messages = [AIMessage(content=final_text)]
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "failed to reconstruct AIMessage from accumulated chunks: %s", exc
                )
                final_messages = []

        return {"messages": final_messages, "usage_metadata": final_meta}

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

        _logger.info(
            "ReAct start: query='%s' user=%s session=%s days=%d max_retries=%d",
            query,
            user_id,
            session_id,
            time_range_days,
            max_retries,
        )

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

                with record_span(
                    "memory", "memory.read_failed",
                    user_id=user_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                ) as span_attrs:
                    span_attrs["status"] = "error"

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

        # ── 流式开关守卫：llm.streaming_enabled + trace_id + event_bus 三项全到位才启用 ──
        # 任一缺失都回到 ainvoke 原路径，保证兼容性。
        from core.observability.middleware import _current_trace
        from core.tasks.context import get_current_message_id
        from core.tasks.events import get_event_bus

        _trace_ctx = _current_trace.get()
        _trace_id = _trace_ctx.trace_id if _trace_ctx is not None else None
        _message_id = get_current_message_id() or _trace_id
        _bus_ready = get_event_bus() is not None
        streaming_on = bool(
            self._settings.llm.streaming_enabled
            and _trace_id
            and _bus_ready
        )

        # ── 重试循环 ──
        for attempt in range(max_retries):
            try:
                agent = self._get_or_build_agent()
                with record_span(
                    "model", "p2p_agent.react"
                ) as _model_attrs:
                    _model_attrs["react_streaming"] = streaming_on
                    if attempt > 0:
                        _model_attrs["retry_attempt"] = attempt + 1
                    if streaming_on:
                        result = await self._astream_react_with_publish(
                            agent,
                            {"messages": invoke_messages},
                            invoke_config,
                            trace_id=_trace_id,  # type: ignore[arg-type]
                            message_id=_message_id,  # type: ignore[arg-type]
                            node="agent_final",
                            span_attrs=_model_attrs,
                        )
                    else:
                        result = await agent.ainvoke(
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

                        with record_span(
                            "memory", "memory.write_failed",
                            user_id=user_id,
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        ) as span_attrs:
                            span_attrs["status"] = "error"

                elapsed_ms: float = (time.monotonic() - start_time) * 1000

                _logger.info(
                    "ReAct done: type=%s anomalies=%d duration=%.1fms attempts=%d",
                    analysis_type.value,
                    summary.get("anomaly_count", 0) if isinstance(summary, dict) else 0,
                    elapsed_ms,
                    attempt + 1,
                )

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
                    created_at=now_cn(),
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

        _logger.error(
            "ReAct exhausted retries: error='%s' attempts=%d duration=%.1fms query='%s'",
            error_msg,
            max_retries,
            elapsed_ms,
            query,
        )

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
            created_at=now_cn(),
        )
