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
from collections import OrderedDict
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
from core.memory import ShortTermMemory
from core.observability import TimingMiddleware
from modules.p2p.model_factory import build_chat_model
from modules.p2p.prompts import build_system_prompt

_logger = get_logger(__name__)


def _naive_summarize(messages: list[dict[str, str]]) -> str:
    """轻量摘要：拼接早期消息的截断片段，避免再起一次 LLM 调用。

    供 ShortTermMemory.compress 使用。专业的摘要可后续替换为 LLM 调用，
    但 MVP 阶段足以保证多轮上下文不无限增长。
    """
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = (msg.get("content") or "").replace("\n", " ").strip()
        if len(content) > 120:
            content = content[:120] + "…"
        parts.append(f"[{role}] {content}")
    return "\n".join(parts)


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
    ) -> None:
        """初始化 P2P Agent。

        Args:
            settings: 全局配置对象，为 None 时自动调用 get_settings() 获取。
            timing_middleware: 链路监控中间件。允许调用方（如 Orchestrator）
                在更外层创建并复用同一份 middleware，确保 trace 能覆盖
                P2PAgent 的导入与构建阶段。为 None 时内部懒建一份。
        """
        self._settings: Settings = settings if settings is not None else get_settings()
        self._agent: Any | None = None
        self._timing_middleware: TimingMiddleware | None = timing_middleware
        # 按 session_id 隔离的短期记忆，使用 OrderedDict 实现 LRU，
        # 防止长时间运行下进程内 session 字典无界增长导致 OOM
        self._short_term_memories: OrderedDict[str, ShortTermMemory] = OrderedDict()
        # Agent 构建 + 短期记忆字典在多线程/多协程下都可能并发访问，
        # 用 RLock 同步避免重复初始化和 OrderedDict 竞态
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

    def clear_short_term_memory(self, session_id: str | None = None) -> int:
        """清理短期记忆。

        Args:
            session_id: 指定会话 ID 时仅清理该会话；为 None 时清空全部会话。

        Returns:
            实际被清理的会话数量。
        """
        with self._lock:
            if session_id is None:
                count = len(self._short_term_memories)
                self._short_term_memories.clear()
                return count
            if session_id in self._short_term_memories:
                del self._short_term_memories[session_id]
                return 1
            return 0

    def _get_short_term_memory(self, session_id: str) -> ShortTermMemory:
        """获取或创建指定会话的短期记忆（LRU 行为）。

        每次访问都把对应 session 移到末尾标记为最近使用；
        当总数超过 ``_MAX_SHORT_TERM_SESSIONS`` 时淘汰最久未用的会话。
        """
        with self._lock:
            stm = self._short_term_memories.get(session_id)
            if stm is not None:
                self._short_term_memories.move_to_end(session_id)
                return stm

            mem_cfg = self._settings.memory
            stm = ShortTermMemory(
                max_messages=mem_cfg.short_term_max_messages,
                summary_threshold=mem_cfg.short_term_summary_threshold,
            )
            self._short_term_memories[session_id] = stm
            # LRU 淘汰
            max_sessions = self._settings.agent_runtime.max_short_term_sessions
            while len(self._short_term_memories) > max_sessions:
                self._short_term_memories.popitem(last=False)
            return stm

    # ------------------------------------------------------------------
    # 构建方法
    # ------------------------------------------------------------------

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
            self._agent = create_agent(
                model=model,
                tools=tools,
                system_prompt=system_prompt,
                name="p2p_agent",
                middleware=[middleware],
            )
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
    ) -> dict[str, Any]:
        """供 Orchestrator 调用的异步入口。

        将 Orchestrator 的参数映射到 analyze()，并将 AnalysisResult
        转换为 Orchestrator 期望的 dict 格式。

        Args:
            analysis_type: 分析类型。
            query: 自然语言查询。
            params: 意图解析提取的额外参数（supplier_id, po_number 等）。
            time_range_days: 分析时间范围（天）。

        Returns:
            包含 anomalies, supplier_kpis, summary, report_markdown,
            completed_tasks, failed_tasks 的字典。
        """
        result = await self.analyze(
            query=query,
            user_id=user_id,
            session_id=session_id,
            time_range_days=time_range_days,
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

        for attempt in range(max_retries):
            try:
                agent = self._get_or_build_agent()

                # 构造带上下文的用户消息
                user_message: str = (
                    f"{query}\n\n"
                    f"[分析参数] 时间范围: 最近 {time_range_days} 天"
                )

                # 注入短期记忆：把同一 session 的历史拼到本轮 messages 之前
                stm = self._get_short_term_memory(session_id)
                history_messages: list[dict[str, str]] = stm.get_context()
                invoke_messages: list[dict[str, str]] = [
                    *history_messages,
                    {"role": "user", "content": user_message},
                ]

                # trace 生命周期由 Orchestrator 在最外层驱动，
                # 此处仅触发 ainvoke，让 middleware 的 model/tool span 自然落入当前 trace
                result: dict[str, Any] = await agent.ainvoke({
                    "messages": invoke_messages,
                })

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

                # 写回短期记忆：本轮 user query + assistant 回复
                stm.add_message("user", user_message)
                if content:
                    stm.add_message("assistant", content)
                # 达到阈值时压缩早期消息（使用简单截断式摘要，无需额外 LLM 调用）
                if stm.needs_compression():
                    stm.compress(_naive_summarize)

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
