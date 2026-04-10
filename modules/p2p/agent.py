"""
P2P Agent 定义。

基于 LangChain 1.2.0 create_agent API 构建 P2P 采购分析智能体，
使用 ChatOpenAI 兼容接口连接 GLM-4 大模型，集成 8 个结构化工具
实现采购订单查询、三路匹配、价格差异分析、付款合规检查和供应商绩效计算。
"""

from __future__ import annotations

import asyncio
import atexit
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
from core.observability import TimingMiddleware, attach_checkpointer_tracing
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
        # 短期记忆由 LangGraph PostgresSaver checkpointer 持久化,以 session_id 作为 thread_id,
        # 每轮会话的历史 messages 由 checkpointer 自动加载/写回,无需进程内字典。
        self._checkpointer: Any | None = None
        self._checkpointer_cm: Any | None = None  # context manager 句柄,用于 atexit 清理
        # Agent 构建 + checkpointer 初始化在多线程/多协程下都可能并发访问,
        # 用 RLock 同步避免重复初始化
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

    def _get_checkpointer(self) -> Any:
        """延迟构建 LangGraph PostgresSaver checkpointer。

        使用项目共用的 PostgreSQL(``settings.postgresql.conninfo``),首次调用时
        进入 context manager 并触发 ``setup()`` 建表。进程退出时通过 atexit
        关闭底层连接。初始化失败时抛出异常,由调用方捕获降级。
        """
        if self._checkpointer is not None:
            return self._checkpointer
        with self._lock:
            if self._checkpointer is not None:
                return self._checkpointer
            from langgraph.checkpoint.postgres import PostgresSaver

            conninfo = self._settings.postgresql.conninfo
            cm = PostgresSaver.from_conn_string(conninfo)
            saver = cm.__enter__()
            try:
                saver.setup()
            except Exception:
                cm.__exit__(None, None, None)
                raise
            # 给 saver 实例打上 checkpoint span 补丁,让 get_tuple / put /
            # put_writes 的耗时与关键元信息(thread_id、n_messages、step、source 等)
            # 落入当前 trace 的 checkpoint 类型 span。
            attach_checkpointer_tracing(saver, self.timing_middleware)
            self._checkpointer_cm = cm
            self._checkpointer = saver
            atexit.register(self._close_checkpointer)
            return saver

    def _close_checkpointer(self) -> None:
        """关闭 checkpointer 的 context manager(atexit 回调,幂等)。"""
        cm = self._checkpointer_cm
        if cm is None:
            return
        self._checkpointer_cm = None
        self._checkpointer = None
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    def clear_short_term_memory(self, session_id: str | None = None) -> int:
        """清理短期记忆(checkpointer 中对应 thread_id 的历史)。

        Args:
            session_id: 指定会话 ID 时仅清理该会话(即 thread_id);
                为 None 时目前不支持一次性清空所有 thread(PostgresSaver 无全量 API),
                返回 0 表示未执行任何操作。

        Returns:
            实际被清理的会话数量。
        """
        if self._checkpointer is None:
            return 0
        if session_id is None:
            # PostgresSaver 未暴露"删除所有 thread"的 API,避免误伤其他表数据,
            # 这里不执行任何操作。需要批量清空请直接 TRUNCATE checkpoint* 表。
            return 0
        try:
            self._checkpointer.delete_thread(session_id)
            return 1
        except Exception as exc:  # noqa: BLE001
            _logger.warning("clear short-term memory failed: %s", exc)
            return 0

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
            # 尝试挂载 PostgresSaver checkpointer 作为短期记忆层。
            # DB 不可用时降级为无 checkpointer 模式,不阻塞 agent 启动。
            checkpointer: Any | None = None
            try:
                checkpointer = self._get_checkpointer()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "postgres checkpointer init failed, short-term memory disabled: %s",
                    exc,
                )
            agent_kwargs: dict[str, Any] = dict(
                model=model,
                tools=tools,
                system_prompt=system_prompt,
                name="p2p_agent",
                middleware=[middleware],
            )
            if checkpointer is not None:
                agent_kwargs["checkpointer"] = checkpointer
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

                # 短期记忆由 PostgresSaver checkpointer 按 thread_id(=session_id)
                # 自动加载历史 messages,这里只需传入本轮新消息。
                invoke_messages: list[tuple[str, str]] = []

                # 注入长期记忆：按 user_id 隔离,按当前 query 做 LIKE 召回 + 可选向量召回。
                # 召回失败(DB 未初始化/不可用)不应阻断主路径,静默降级。
                long_term_snippets: list[dict[str, Any]] = []
                if self._settings.memory.long_term_enabled:
                    try:
                        from core.memory import get_long_term_memory

                        ltm = get_long_term_memory()
                        max_retrieved = self._settings.memory.long_term_max_retrieved
                        # search_memories 内部已做 hybrid 召回（稀疏 + 稠密 + RRF）
                        long_term_snippets = ltm.search_memories(
                            user_id=user_id, query=query, limit=max_retrieved
                        )
                    except Exception as exc:  # noqa: BLE001
                        _logger.debug("long-term memory retrieval skipped: %s", exc)

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

                # trace 生命周期由 Orchestrator 在最外层驱动,
                # 此处仅触发 ainvoke,让 middleware 的 model/tool span 自然落入当前 trace。
                # thread_id 即 session_id,PostgresSaver 会据此加载/写回短期记忆历史。
                invoke_config: dict[str, Any] = {
                    "configurable": {"thread_id": session_id}
                }
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
                # 失败不阻塞主路径。
                if content and self._settings.memory.long_term_enabled:
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
                        _logger.debug("long-term memory save skipped: %s", exc)

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
