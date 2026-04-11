"""
Orchestrator 编排器模块。

负责接收分析请求、解析意图、路由到 DAG 执行或 Agent ReAct 执行，
并将结果封装为统一的 AnalysisResult 返回。

路由策略（共存模式）：
- Level 1/2 命中 → 加载静态 DAG 模板 → DAG Executor 并行执行 → ReportAgent 汇总
- Level 3 兜底 → P2PAgent ReAct 自主执行（保留原有行为）
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from api.schemas.analysis import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    ErrorInfo,
)
from config.settings import Settings, get_settings
from core.logging_utils import get_logger
from core.observability import TimingMiddleware
from core.orchestrator.router import IntentRouter

_logger = get_logger(__name__)


class Orchestrator:
    """P2P 分析编排器。

    协调意图解析、DAG/Agent 调度和结果封装的核心组件。
    采用延迟初始化策略，避免启动时加载重量级依赖。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        if settings is None:
            settings = get_settings()
        self._settings: Settings = settings
        self._agent: Any = None
        self._dag_executor: Any = None
        self._report_agent: Any = None
        self._init_components()

    def _init_components(self) -> None:
        """初始化轻量级组件。"""
        self._intent_router: IntentRouter = IntentRouter(settings=self._settings)
        self._timing_middleware: TimingMiddleware = TimingMiddleware(
            agent_name="p2p_agent"
        )

    def clear_short_term_memory(self, session_id: str | None = None) -> int:
        """清理 P2PAgent 的短期记忆。"""
        if self._agent is None:
            return 0
        return self._agent.clear_short_term_memory(session_id)

    def _persist_report(self, result: AnalysisResult) -> None:
        """将成功的分析结果写入长期记忆。"""
        try:
            from core.memory import get_long_term_memory

            ltm = get_long_term_memory()
            ltm.save_report(
                user_id=result.user_id,
                session_id=result.session_id,
                query=result.query,
                analysis_type=result.analysis_type.value,
                result_json=result.model_dump_json(),
                report_markdown=result.report_markdown or "",
                anomaly_count=len(result.anomalies),
                report_id=result.report_id,
            )
        except Exception as exc:
            _logger.warning(
                "persist report to long-term memory failed: %s (report_id=%s)",
                exc,
                result.report_id,
                exc_info=True,
            )

    # ── 延迟初始化 ─────────────────────────────────────────────────

    @property
    def _lazy_agent(self) -> Any:
        """延迟初始化 P2PAgent 实例（Level 3 ReAct 兜底）。"""
        if self._agent is None:
            from modules.p2p.agent import P2PAgent

            self._agent = P2PAgent(
                settings=self._settings,
                timing_middleware=self._timing_middleware,
            )
        return self._agent

    @property
    def _lazy_dag_executor(self) -> Any:
        """延迟初始化 DAG Executor（Level 1/2 DAG 执行）。"""
        if self._dag_executor is None:
            from core.orchestrator.dag.executor import DAGExecutor
            from core.orchestrator.dag.registry import build_default_registry
            from modules.p2p.report_agent import ReportAgent

            registry = build_default_registry()
            if self._report_agent is None:
                self._report_agent = ReportAgent(settings=self._settings)
            self._dag_executor = DAGExecutor(
                registry=registry,
                report_agent=self._report_agent,
            )
        return self._dag_executor

    # ── 短期记忆上下文读取 ─────────────────────────────────────────

    def _load_session_context(self, session_id: str) -> dict[str, Any]:
        """从 checkpointer 读取当前 session 的对话历史，提取上下文信息。

        返回字典包含：
        - context_summary: 最近一轮 AI 回复的摘要（截断到 500 字符）
        - entities: 从历史中提取的实体（po_number / supplier_id）
        - has_history: 是否有历史对话

        读取失败返回空上下文，不阻塞主流程。
        """
        from core.observability.middleware import record_span
        from core.orchestrator.router import _extract_params

        empty: dict[str, Any] = {"has_history": False, "context_summary": "", "entities": {}}

        with record_span("checkpoint", "load_session_context") as span_attrs:
            span_attrs["session_id"] = session_id

            try:
                agent = self._lazy_agent
                checkpointer = agent._checkpointer
                if checkpointer is None:
                    span_attrs["status"] = "skipped"
                    span_attrs["reason"] = "checkpointer not available"
                    return empty

                config = {"configurable": {"thread_id": session_id}}
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
                    extracted = _extract_params(text)
                    for key, val in extracted.items():
                        if key not in entities and val:
                            entities[key] = val

                context_summary = last_ai[:500] if last_ai else ""

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

    # ── 指代消解 ─────────────────────────────────────────────────────

    @staticmethod
    def _resolve_references(
        query: str, session_ctx: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """指代消解：将 query 中的指代词替换为具体实体，返回 (增强 query, 选择性实体)。

        规则：
        - "这个/该/上述 + po/订单/采购订单" → 只关联 po_number
        - "这个/该/上述 + 供应商" → 只关联 supplier_id
        - 无指代但有历史 → 全部补充（保持兼容）
        - 无历史 → 原样返回

        返回：
        - enhanced_query: 指代词替换后的 query（供路由器使用）
        - relevant_entities: 与指代相关的实体子集（供参数补充使用）
        """
        import re

        if not session_ctx.get("has_history"):
            return query, {}

        entities = session_ctx.get("entities", {})
        if not entities:
            return query, {}

        enhanced = query
        relevant: dict[str, Any] = {}

        # 指代前缀：覆盖中文常用的指示词 + 量词化指代 + 时间指代
        _REF_PREFIX = (
            r"(?:这个|这条|这笔|这张|这批|这份|"
            r"该|那个|那条|那笔|那张|此|"
            r"上述|上面的|上面提到的|前面的|刚才的|之前的)"
        )

        # 指代模式表：(正则, 实体键, 替换文本模板)
        _REF_PATTERNS: list[tuple[str, str, str]] = [
            # PO / 订单
            (
                _REF_PREFIX + r"\s*(?:po|PO|订单|采购订单|采购单)",
                "po_number",
                "采购订单 {val}",
            ),
            # 供应商
            (
                _REF_PREFIX + r"\s*(?:供应商|vendor|supplier)",
                "supplier_id",
                "供应商 {val}",
            ),
            # 付款单 / 支付单
            (
                _REF_PREFIX + r"\s*(?:支付单|付款单|付款|payment|PAY|pay)",
                "payment_number",
                "付款单 {val}",
            ),
            # 发票
            (
                _REF_PREFIX + r"\s*(?:发票|invoice|INV|inv)",
                "invoice_number",
                "发票 {val}",
            ),
            # 收货单
            (
                _REF_PREFIX + r"\s*(?:收货单|收货|RCV|rcv|goods receipt)",
                "receipt_number",
                "收货单 {val}",
            ),
        ]

        for pattern, entity_key, replace_tpl in _REF_PATTERNS:
            ref_match = re.search(pattern, enhanced, re.IGNORECASE)
            if ref_match and entities.get(entity_key):
                val = entities[entity_key]
                replacement = replace_tpl.format(val=val)
                enhanced = enhanced[:ref_match.start()] + replacement + enhanced[ref_match.end():]
                relevant[entity_key] = val

        # 通用指代但未匹配到具体实体类型："分析这个的风险" / "它的情况"
        _GENERIC_REF = (
            r"(?:这个|这条|这笔|这张|这批|这份|"
            r"该|那个|那条|那笔|那张|此|它|"
            r"上述|上面的|上面提到的|前面的|刚才的|之前的)"
        )
        if not relevant:
            generic_ref = re.search(_GENERIC_REF + r"(?:的)", query)
            if generic_ref:
                relevant = entities.copy()

        # 无任何指代检测命中，但有历史 → 全部补充（保持向后兼容）
        if not relevant and not re.search(_GENERIC_REF, query):
            relevant = entities.copy()

        return enhanced, relevant

    # ── 核心编排 ─────────────────────────────────────────────────────

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        """执行分析请求的完整编排流程。

        路由策略：
        0. 读取短期记忆上下文（从 checkpointer 提取历史实体）。
        1. 指代消解：将 query 中的"这个po"等指代词替换为具体实体。
        2. IntentRouter 三级路由解析增强后的 query。
        3. 选择性补充上下文实体到参数。
        4. Level 1/2 命中且有 DAG 模板 → DAG Executor 并行执行。
        5. Level 3 或无 DAG 模板 → P2PAgent ReAct 执行。
        """
        start_time = time.monotonic()
        report_id = str(uuid.uuid4())
        session_id = request.session_id or str(uuid.uuid4())

        timing_middleware = self._timing_middleware
        trace_id = timing_middleware.start_run(
            session_id=session_id, user_id=request.user_id
        )
        trace_status: str = "success"
        trace_error: str | None = None

        try:
            # 0. 读取短期记忆上下文（提取历史实体）
            session_ctx = await asyncio.to_thread(
                self._load_session_context, session_id
            )

            # 1. 指代消解：将指代词替换为具体实体，确定关联的实体子集
            enhanced_query, relevant_entities = self._resolve_references(
                request.query, session_ctx
            )
            if enhanced_query != request.query:
                _logger.info(
                    "reference resolved: '%s' → '%s' (entities: %s)",
                    request.query,
                    enhanced_query,
                    list(relevant_entities.keys()),
                )

            # 2. 意图解析（用增强后的 query 路由）
            signal = self._intent_router.route(enhanced_query)
            analysis_type: AnalysisType = request.analysis_type or self._intent_router.resolve_type(signal)

            # 3. 合并参数
            parsed_params = signal.entities.copy()
            time_range_days: int = (
                request.time_range_days
                or signal.time_range_days
                or self._settings.analysis.default_time_range_days
            )
            parsed_params["days"] = time_range_days

            # 4. 选择性补充上下文实体（只补充指代消解确定的关联实体）
            for key, val in relevant_entities.items():
                if key not in parsed_params or not parsed_params[key]:
                    parsed_params[key] = val
                    _logger.debug(
                        "entity '%s'='%s' inherited from session context", key, val
                    )

            # 5. 路由决策
            # - L1/L2 命中且非 COMPREHENSIVE → DAG 执行
            # - COMPREHENSIVE + 有具体实体 → 实体维度 DAG
            # - 其余 → ReAct 兜底
            has_entity = bool(
                parsed_params.get("po_number")
                or parsed_params.get("supplier_id")
                or parsed_params.get("payment_number")
                or parsed_params.get("invoice_number")
                or parsed_params.get("receipt_number")
            )
            use_dag = (
                (signal.route_level in (1, 2) and analysis_type != AnalysisType.COMPREHENSIVE)
                or (analysis_type == AnalysisType.COMPREHENSIVE and has_entity)
            )

            if use_dag:
                result = await self._execute_dag(
                    analysis_type=analysis_type,
                    params=parsed_params,
                    query=request.query,
                    report_id=report_id,
                    trace_id=trace_id,
                    user_id=request.user_id,
                    session_id=session_id,
                    time_range_days=time_range_days,
                    start_time=start_time,
                    signal=signal,
                )
            else:
                result = await self._execute_react(
                    analysis_type=analysis_type,
                    params=parsed_params,
                    query=request.query,
                    report_id=report_id,
                    trace_id=trace_id,
                    user_id=request.user_id,
                    session_id=session_id,
                    time_range_days=time_range_days,
                    start_time=start_time,
                    signal=signal,
                )

            # 5. 持久化
            await asyncio.to_thread(self._persist_report, result)
            return result

        except Exception as exc:
            trace_status = "error"
            trace_error = f"{type(exc).__name__}: {exc}"
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return AnalysisResult(
                report_id=report_id,
                trace_id=trace_id,
                status=AnalysisStatus.FAILED,
                analysis_type=request.analysis_type or AnalysisType.COMPREHENSIVE,
                query=request.query,
                user_id=request.user_id,
                session_id=session_id,
                time_range="",
                error=ErrorInfo(
                    code="ORCHESTRATOR_ERROR",
                    message=str(exc),
                ),
                duration_ms=duration_ms,
            )
        finally:
            timing_middleware.finish_run(status=trace_status, error=trace_error)

    # ── DAG 执行路径 ────────────────────────────────────────────────

    async def _execute_dag(
        self,
        analysis_type: AnalysisType,
        params: dict[str, Any],
        query: str,
        report_id: str,
        trace_id: str,
        user_id: str,
        session_id: str,
        time_range_days: int,
        start_time: float,
        signal: Any,
    ) -> AnalysisResult:
        """通过 DAG Executor 执行分析。"""
        from core.orchestrator.dag.templates import load_dag_template
        from core.orchestrator.dag.validator import DAGValidator

        dag_tasks = load_dag_template(analysis_type, params)
        if dag_tasks is None:
            # 无对应模板，降级到 ReAct
            _logger.info("no DAG template for %s, fallback to ReAct", analysis_type.value)
            return await self._execute_react(
                analysis_type=analysis_type,
                params=params,
                query=query,
                report_id=report_id,
                trace_id=trace_id,
                user_id=user_id,
                session_id=session_id,
                time_range_days=time_range_days,
                start_time=start_time,
                signal=signal,
            )

        # 校验 DAG
        executor = self._lazy_dag_executor
        validator = DAGValidator(executor._registry)
        is_valid, error = validator.validate(dag_tasks)
        if not is_valid:
            _logger.warning("DAG validation failed: %s, fallback to ReAct", error)
            return await self._execute_react(
                analysis_type=analysis_type,
                params=params,
                query=query,
                report_id=report_id,
                trace_id=trace_id,
                user_id=user_id,
                session_id=session_id,
                time_range_days=time_range_days,
                start_time=start_time,
                signal=signal,
            )

        _logger.info(
            "executing DAG: type=%s tasks=%d route_level=%d confidence=%.3f",
            analysis_type.value,
            len(dag_tasks),
            signal.route_level,
            signal.confidence,
        )

        dag_result = await executor.execute(dag_tasks)
        duration_ms = (time.monotonic() - start_time) * 1000.0

        # DAG 执行完成后，将 query + 报告写入短期记忆（checkpointer），
        # 确保同一 session 的后续请求（无论 DAG 还是 ReAct）能看到本次对话历史。
        report_text = dag_result.get("report", "")
        await self._save_dag_to_short_term_memory(
            query=query,
            response=report_text,
            session_id=session_id,
            time_range_days=time_range_days,
        )

        status = AnalysisStatus.SUCCESS
        if dag_result["status"] == "failed":
            status = AnalysisStatus.FAILED
        elif dag_result["status"] == "partial":
            status = AnalysisStatus.PARTIAL_SUCCESS

        failed_list = [
            f"{tid}: {err}" for tid, err in dag_result.get("failed_tasks", {}).items()
        ]

        return AnalysisResult(
            report_id=report_id,
            trace_id=trace_id,
            status=status,
            analysis_type=analysis_type,
            query=query,
            user_id=user_id,
            session_id=session_id,
            time_range=f"最近 {time_range_days} 天",
            report_markdown=dag_result.get("report", ""),
            completed_tasks=dag_result.get("completed_tasks", []),
            failed_tasks=failed_list,
            summary={
                "route_type": "DAG",
                "route_level": signal.route_level,
                "route_confidence": signal.confidence,
                "route_reasoning": signal.reasoning,
                "dag_duration_sec": dag_result.get("duration_sec", 0),
            },
            duration_ms=duration_ms,
        )

    # ── DAG 短期记忆写入 ────────────────────────────────────────────

    async def _save_dag_to_short_term_memory(
        self,
        query: str,
        response: str,
        session_id: str,
        time_range_days: int,
    ) -> None:
        """将 DAG 执行的 query + response 写入 checkpointer 短期记忆。

        构造与 ReAct 路径一致的 HumanMessage + AIMessage 对，
        通过 checkpointer.put 写入，确保同 session 后续请求能读到历史。
        写入失败不阻塞主流程（与 ReAct 路径 checkpointer 降级策略一致）。
        """
        from core.observability.middleware import record_span

        with record_span("checkpoint", "dag_short_term_write") as span_attrs:
            span_attrs["session_id"] = session_id
            span_attrs["query_length"] = len(query)
            span_attrs["response_length"] = len(response)

            try:
                # 延迟获取 checkpointer（通过 P2PAgent 共享实例）
                agent = self._lazy_agent
                checkpointer = agent._checkpointer
                if checkpointer is None:
                    span_attrs["status"] = "skipped"
                    span_attrs["reason"] = "checkpointer not available"
                    return

                from langchain_core.messages import AIMessage, HumanMessage

                user_msg = HumanMessage(
                    content=f"{query}\n\n[分析参数] 时间范围: 最近 {time_range_days} 天"
                )
                ai_msg = AIMessage(content=response or "(DAG 分析完成，报告为空)")

                config = {"configurable": {"thread_id": session_id}}

                # 读取现有 checkpoint（可能为 None）
                existing = checkpointer.get_tuple(config)

                if existing and existing.checkpoint:
                    # 在已有 checkpoint 基础上追加消息
                    checkpoint = existing.checkpoint
                    channel_values = checkpoint.get("channel_values", {})
                    messages = channel_values.get("messages", [])
                    messages.extend([user_msg, ai_msg])
                    channel_values["messages"] = messages
                    checkpoint["channel_values"] = channel_values

                    import uuid as _uuid
                    checkpoint["id"] = str(_uuid.uuid4())

                    metadata = {
                        "source": "dag",
                        "step": (existing.metadata or {}).get("step", 0) + 1,
                        "writes": None,
                    }
                    await asyncio.to_thread(
                        checkpointer.put, config, checkpoint, metadata, {}
                    )
                else:
                    # 首次写入，构造新 checkpoint
                    import uuid as _uuid
                    checkpoint = {
                        "v": 1,
                        "id": str(_uuid.uuid4()),
                        "ts": None,
                        "channel_values": {"messages": [user_msg, ai_msg]},
                        "channel_versions": {},
                        "versions_seen": {},
                    }
                    metadata = {"source": "dag", "step": 1, "writes": None}
                    await asyncio.to_thread(
                        checkpointer.put, config, checkpoint, metadata, {}
                    )

                span_attrs["status"] = "ok"
                span_attrs["n_messages"] = 2
                _logger.debug(
                    "DAG result saved to short-term memory: session=%s", session_id
                )

            except Exception as exc:
                span_attrs["status"] = "error"
                span_attrs["error"] = str(exc)
                _logger.warning(
                    "DAG short-term memory write failed (non-blocking): %s", exc
                )

    # ── ReAct 执行路径（原有逻辑） ──────────────────────────────────

    async def _execute_react(
        self,
        analysis_type: AnalysisType,
        params: dict[str, Any],
        query: str,
        report_id: str,
        trace_id: str,
        user_id: str,
        session_id: str,
        time_range_days: int,
        start_time: float,
        signal: Any = None,
    ) -> AnalysisResult:
        """通过 P2PAgent ReAct 模式执行分析（Level 3 兜底）。"""
        agent_result: dict[str, Any] = await self._lazy_agent.run(
            analysis_type=analysis_type,
            query=query,
            params=params,
            time_range_days=time_range_days,
            user_id=user_id,
            session_id=session_id,
        )

        duration_ms = (time.monotonic() - start_time) * 1000.0

        # 合并路由监控信息到 summary
        route_info: dict[str, Any] = {"route_type": "ReAct"}
        if signal is not None:
            route_info["route_level"] = signal.route_level
            route_info["route_confidence"] = signal.confidence
            route_info["route_reasoning"] = signal.reasoning

        summary = {**agent_result.get("summary", {}), **route_info}

        return AnalysisResult(
            report_id=report_id,
            trace_id=trace_id,
            status=AnalysisStatus.SUCCESS,
            analysis_type=analysis_type,
            query=query,
            user_id=user_id,
            session_id=session_id,
            time_range=f"最近 {time_range_days} 天",
            anomalies=agent_result.get("anomalies", []),
            supplier_kpis=agent_result.get("supplier_kpis", []),
            summary=summary,
            report_markdown=agent_result.get("report_markdown", ""),
            completed_tasks=agent_result.get("completed_tasks", []),
            failed_tasks=agent_result.get("failed_tasks", []),
            duration_ms=duration_ms,
        )
