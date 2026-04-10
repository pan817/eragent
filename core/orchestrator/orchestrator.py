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

    # ── 核心编排 ─────────────────────────────────────────────────────

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        """执行分析请求的完整编排流程。

        路由策略：
        1. IntentRouter 三级路由解析意图。
        2. Level 1/2 命中且有 DAG 模板 → DAG Executor 并行执行。
        3. Level 3 或无 DAG 模板 → P2PAgent ReAct 执行。
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
            # 1. 意图解析（三级路由）
            signal = self._intent_router.route(request.query)
            analysis_type: AnalysisType = request.analysis_type or self._intent_router.resolve_type(signal)

            # 2. 合并参数
            parsed_params = signal.entities.copy()
            time_range_days: int = (
                request.time_range_days
                or signal.time_range_days
                or self._settings.analysis.default_time_range_days
            )
            parsed_params["days"] = time_range_days

            # 3. 路由决策：L1/L2 命中且有 DAG 模板 → DAG 执行
            use_dag = (
                signal.route_level in (1, 2)
                and analysis_type != AnalysisType.COMPREHENSIVE
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
                )

            # 4. 持久化
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
            summary=agent_result.get("summary", {}),
            report_markdown=agent_result.get("report_markdown", ""),
            completed_tasks=agent_result.get("completed_tasks", []),
            failed_tasks=agent_result.get("failed_tasks", []),
            duration_ms=duration_ms,
        )
