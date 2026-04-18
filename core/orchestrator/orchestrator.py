"""
Orchestrator 编排器模块。

负责接收分析请求、解析意图、路由到 DAG 执行或 Agent ReAct 执行，
并将结果封装为统一的 AnalysisResult 返回。

路由策略（共存模式）：
- Level 1/2 命中 → 加载静态 DAG 模板 → DAG Executor 并行执行 → ReportAgent 汇总
- Level 3 兜底 → P2PAgent ReAct 自主执行（保留原有行为）
"""

from __future__ import annotations

import atexit
import asyncio
import threading
import time
import uuid
from typing import Any

from api.schemas.analysis import AnalysisRequest
from api.schemas.domain import (
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    ErrorInfo,
)
from config.settings import Settings, get_settings
from core.logging_utils import get_logger
from core.observability import TimingMiddleware
from core.observability.streaming import publish_stage as _publish_stage
from core.tasks.stream_utils import strip_think_tags as _strip_think_tags
from core.orchestrator.router import IntentRouter

_logger = get_logger(__name__)


# 概览查询触发词（批 5）：需要同时包含"采购相关词"和"概览意图词"
_OVERVIEW_PROCUREMENT_WORDS = {"采购", "procurement", "采购订单"}
_OVERVIEW_INTENT_WORDS = {
    "最近", "近期", "概览", "总体", "总览", "整体", "全面",
    "现状", "情况", "怎么样", "近况", "健康度",
}


def _matches_overview_query(query: str) -> bool:
    """判断 query 是否为概览类查询（需同时命中采购词 + 概览意图词）。"""
    q = query.lower()
    has_procurement = any(w in q for w in _OVERVIEW_PROCUREMENT_WORDS)
    has_intent = any(w in q for w in _OVERVIEW_INTENT_WORDS)
    return has_procurement and has_intent


def _publish_stage_safe(name: str, attrs: dict[str, Any] | None = None) -> None:
    """orchestrator 内用的 stage 事件发布器：失败吞掉，不影响分析主流程。"""
    try:
        _publish_stage(name, attrs)
    except Exception:  # noqa: BLE001
        _logger.info("publish_stage failed", exc_info=True)

# 输出模式 → prompt 后缀
# 输出模式覆盖指令：拼接在 _REPORT_PROMPT 末尾，声明优先级高于上文"报告要求"。
# - "detailed" 保持空串是刻意约定：完全沿用 _REPORT_PROMPT 的默认 4 段结构，无需额外覆盖；
#   改为非空反而会在基础 prompt 上重复一次，浪费 token。
# - "brief" / "table" 给出具体结构/字数约束，覆盖基础 prompt 的 4 段规定。
# 取值由 AnalysisRequest.output_mode 的 Pydantic pattern 校验，非法值在 API 层 400，
# 此字典不承担二次校验职责。
from core.orchestrator.prompts import (  # noqa: E402
    build_output_mode_prompts as _build_output_mode_prompts,
    render_intent_kind_template as _render_intent_kind_template,
)


def _resolve_time_range(time_range: str | None) -> int | None:
    """将 time_range 字符串转换为天数。

    Args:
        time_range: 如 "7d"/"30d"/"this_month"/"last_month"，None 表示未传。

    Returns:
        等效天数，None 表示未传或无法解析。
    """
    if not time_range:
        return None

    import re
    from datetime import date, timedelta

    # Nd 格式
    m = re.match(r"^(\d+)d$", time_range)
    if m:
        return int(m.group(1))

    today = date.today()

    if time_range == "this_month":
        # 本月1日到今天
        return (today - today.replace(day=1)).days + 1

    if time_range == "last_month":
        # 上月1日到今天（近似覆盖，包含本月数据）
        first_of_this_month = today.replace(day=1)
        first_of_last_month = (first_of_this_month - timedelta(days=1)).replace(day=1)
        return (today - first_of_last_month).days

    return None


class Orchestrator:
    """分析编排器。

    协调意图解析、DAG/Agent 调度和结果封装的核心组件。
    通过 ModuleProvider 获取业务模块能力，不直接 import 具体模块代码。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        provider: Any | None = None,
    ) -> None:
        if settings is None:
            settings = get_settings()
        self._settings: Settings = settings
        self._provider: Any = provider
        self._agent: Any = None
        self._dag_executor: Any = None
        self._report_agent: Any = None
        self._lock = threading.RLock()
        self._init_components()
        from core.memory.short_term import ShortTermMemory
        self._short_term = ShortTermMemory(settings=self._settings)

    def _init_components(self) -> None:
        """初始化轻量级组件。"""
        self._intent_router: IntentRouter = IntentRouter(settings=self._settings)
        self._timing_middleware: TimingMiddleware = TimingMiddleware(
            agent_name="p2p_agent"
        )

    # ── 短期记忆（委托 core/memory/short_term.py）─────────────────

    def _get_checkpointer(self) -> Any:
        """延迟构建 checkpointer（委托到 ShortTermMemory）。"""
        return self._short_term.get_checkpointer()

    def _close_checkpointer(self) -> None:
        """关闭 checkpointer（委托到 ShortTermMemory）。"""
        self._short_term.close()

    def _ensure_checkpointer(self) -> Any | None:
        """获取 checkpointer，失败返回 None（委托到 ShortTermMemory）。"""
        return self._short_term.ensure_checkpointer()

    def clear_short_term_memory(self, session_id: str | None = None) -> int:
        """清理短期记忆（委托到 ShortTermMemory）。"""
        return self._short_term.clear(session_id)

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
                trace_id=result.trace_id or None,
            )
        except Exception as exc:
            _logger.warning(
                "persist report to long-term memory failed: %s (report_id=%s)",
                exc,
                result.report_id,
                exc_info=True,
            )

    async def _save_session_entities(
        self,
        session_id: str,
        parsed_params: dict[str, Any],
        result: AnalysisResult,
    ) -> None:
        """合并路由阶段 + 执行结果中的实体，写入 session_entities。"""
        entity_keys = {"po_number", "supplier_id", "invoice_number",
                       "payment_number", "receipt_number"}
        # 来源 A：路由阶段已解析的实体（含继承 + 级联补充）
        session_entities = {
            k: v for k, v in parsed_params.items()
            if k in entity_keys and v
        }
        # 来源 B：从执行结果报告中补充提取新实体
        if result.report_markdown:
            from core.orchestrator.router import _extract_params
            result_entities = _extract_params(
                result.report_markdown,
                self._settings.analysis.entity_patterns,
            )
            for k, v in result_entities.items():
                if k in entity_keys and v and k not in session_entities:
                    session_entities[k] = v

        if session_entities:
            await asyncio.to_thread(
                self._short_term.save_entity_context,
                session_id, session_entities,
            )

    # ── 延迟初始化 ─────────────────────────────────────────────────

    @property
    def _lazy_agent(self) -> Any:
        """延迟初始化 Agent 实例（Level 3 ReAct 兜底）。"""
        if self._agent is None:
            if self._provider is not None:
                self._agent = self._provider.get_agent(
                    settings=self._settings,
                    timing_middleware=self._timing_middleware,
                    checkpointer=self._ensure_checkpointer(),
                )
            else:
                # 兼容无 Provider 的测试场景
                from modules.p2p.agent import P2PAgent

                self._agent = P2PAgent(
                    settings=self._settings,
                    timing_middleware=self._timing_middleware,
                    checkpointer=self._ensure_checkpointer(),
                )
        return self._agent

    @property
    def _lazy_dag_executor(self) -> Any:
        """延迟初始化 DAG Executor（Level 1/2 DAG 执行）。"""
        if self._dag_executor is None:
            from core.orchestrator.dag.executor import DAGExecutor
            from core.orchestrator.dag.case_store import DAGCaseStore

            if self._provider is not None:
                from core.orchestrator.dag.registry import build_registry_from_provider
                registry = build_registry_from_provider(self._provider)
            else:
                from core.orchestrator.dag.registry import build_default_registry
                registry = build_default_registry()
            if self._report_agent is None:
                if self._provider is not None:
                    self._report_agent = self._provider.get_report_agent(
                        settings=self._settings,
                    )
                else:
                    from modules.p2p.report_agent import ReportAgent

                    self._report_agent = ReportAgent(settings=self._settings)
            case_store = DAGCaseStore(settings=self._settings)
            self._dag_executor = DAGExecutor(
                registry=registry,
                report_agent=self._report_agent,
                case_store=case_store,
            )
        return self._dag_executor

    # ── 实体处理（委托 core/orchestrator/entity.py）──────────────────

    async def _enrich_entities(self, params: dict[str, Any]) -> None:
        """验证并补充实体关联（委托到 entity 模块）。"""
        from core.orchestrator.entity import enrich_entities
        await enrich_entities(params, provider=self._provider)

    @staticmethod
    def _resolve_references(
        query: str, session_ctx: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """指代消解（委托到 entity 模块）。"""
        from core.orchestrator.entity import resolve_references
        return resolve_references(query, session_ctx)

    def _load_session_context(self, session_id: str) -> dict[str, Any]:
        """从 checkpointer 读取上下文（委托到 ShortTermMemory）。"""
        return self._short_term.load_session_context(session_id)

    # ── 核心编排 ─────────────────────────────────────────────────────

    async def analyze(
        self,
        request: AnalysisRequest,
        *,
        trace_id: str | None = None,
    ) -> AnalysisResult:
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
            session_id=session_id, user_id=request.user_id, trace_id=trace_id
        )
        trace_status: str = "success"
        trace_error: str | None = None

        timeout = self._settings.analysis.response_timeout_seconds

        _logger.info(
            "analyze start: query='%s' user=%s session=%s output_mode=%s "
            "analysis_type=%s report_id=%s",
            request.query,
            request.user_id,
            session_id,
            request.output_mode,
            request.analysis_type.value if request.analysis_type else "auto",
            report_id,
        )

        try:
            result = await asyncio.wait_for(
                self._analyze_inner(
                    request=request,
                    start_time=start_time,
                    report_id=report_id,
                    session_id=session_id,
                    timing_middleware=timing_middleware,
                    trace_id=trace_id,
                ),
                timeout=timeout,
            )
            _logger.info(
                "analyze done: status=%s duration=%.1fms report_id=%s "
                "type=%s anomalies=%d",
                result.status.value,
                result.duration_ms,
                result.report_id,
                result.analysis_type.value,
                len(result.anomalies or []),
            )
            return result
        except asyncio.TimeoutError:
            trace_status = "error"
            duration_ms = (time.monotonic() - start_time) * 1000.0
            trace_error = (
                f"TimeoutError: 分析响应超时 "
                f"(elapsed={duration_ms:.0f}ms, limit={timeout}s)"
            )
            _logger.error(
                "analyze timeout: elapsed=%.0fms limit=%.0fs query='%s'",
                duration_ms,
                timeout,
                request.query,
            )
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
                    code="RESPONSE_TIMEOUT",
                    message=f"分析响应超时（限制 {timeout}s）",
                ),
                duration_ms=duration_ms,
            )
        except Exception as exc:
            from core.observability.tracing import format_error_chain

            trace_status = "error"
            trace_error = format_error_chain(exc)
            duration_ms = (time.monotonic() - start_time) * 1000.0
            _logger.error(
                "analyze failed: %s: %s elapsed=%.0fms query='%s'",
                type(exc).__name__,
                exc,
                duration_ms,
                request.query,
                exc_info=True,
            )
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

    async def _analyze_inner(
        self,
        *,
        request: AnalysisRequest,
        start_time: float,
        report_id: str,
        session_id: str,
        timing_middleware: Any,
        trace_id: str,
    ) -> AnalysisResult:
        """analyze 的实际执行逻辑（被 wait_for 包裹以支持整体超时）。"""
        try:
            # 0. 读取短期记忆上下文（提取历史实体）
            session_ctx = await asyncio.to_thread(
                self._load_session_context, session_id
            )

            # 0.5 检测是否为非分析意图（回溯/闲聊等）
            from core.orchestrator.router import _is_non_analysis_query
            is_recall = _is_non_analysis_query(request.query)

            # 1. 指代消解：非分析意图时跳过实体继承
            if is_recall:
                enhanced_query = request.query
                relevant_entities: dict[str, Any] = {}
                _logger.info(
                    "non-analysis query detected, skip entity inheritance: '%s'",
                    request.query,
                )
            else:
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

            # 2. 意图解析（用增强后的 query 路由，传入角色偏好）
            signal = self._intent_router.route(
                enhanced_query, analyst_role=request.analyst_role
            )

            # 2.5 按 intent_kind 早退路由：CLARIFICATION / META / CHITCHAT /
            # OUT_OF_SCOPE 都不触发 DAG / ReAct，由模板生成响应即可。
            # ANALYSIS / DATA_LOOKUP / RECALL 继续走完整执行路径。
            # 用户显式指定 analysis_type 时跳过早退（视为强制走分析路径）。
            from core.orchestrator.signal import IntentKind as _IntentKind

            if request.analysis_type is None and signal.intent_kind in (
                _IntentKind.CLARIFICATION,
                _IntentKind.META,
                _IntentKind.CHITCHAT,
                _IntentKind.OUT_OF_SCOPE,
            ):
                duration_ms = (time.monotonic() - start_time) * 1000.0
                _logger.info(
                    "intent_kind=%s early return: query='%s' confidence=%.3f",
                    signal.intent_kind.value,
                    request.query,
                    signal.confidence,
                )
                _publish_stage_safe(
                    "intent_resolved",
                    {
                        "analysis_type": signal.intent_kind.value,
                        "route_level": signal.route_level,
                        "confidence": signal.confidence,
                    },
                )
                return AnalysisResult(
                    report_id=report_id,
                    trace_id=trace_id,
                    status=AnalysisStatus.SUCCESS,
                    analysis_type=AnalysisType.COMPREHENSIVE,
                    query=request.query,
                    user_id=request.user_id,
                    session_id=session_id,
                    time_range="",
                    summary={
                        "route_type": signal.intent_kind.value,
                        "route_level": signal.route_level,
                        "route_confidence": signal.confidence,
                        "route_reasoning": signal.reasoning,
                        "missing_params": signal.missing_params,
                    },
                    report_markdown=_render_intent_kind_template(signal),
                    duration_ms=duration_ms,
                )

            analysis_type: AnalysisType = request.analysis_type or self._intent_router.resolve_type(signal)

            # 用户显式指定 analysis_type → 强制视为 ANALYSIS 路径，覆盖 LLM 判断
            # 否则会出现 "用户指定 THREE_WAY_MATCH 但 intent_kind=DATA_LOOKUP
            # 导致走 ReAct 而非 DAG" 的不一致组合。
            if request.analysis_type is not None and signal.intent_kind != _IntentKind.ANALYSIS:
                _logger.info(
                    "explicit analysis_type=%s overrides intent_kind=%s → ANALYSIS",
                    analysis_type.value,
                    signal.intent_kind.value,
                )
                signal.intent_kind = _IntentKind.ANALYSIS
                signal.keywords = [analysis_type.value]

            _logger.info(
                "intent resolved: type=%s kind=%s level=L%d confidence=%.3f keywords=%s",
                analysis_type.value,
                signal.intent_kind.value,
                signal.route_level,
                signal.confidence,
                signal.keywords,
            )
            # SSE 阶段事件：意图已解析
            _publish_stage_safe(
                "intent_resolved",
                {
                    "analysis_type": analysis_type.value,
                    "route_level": signal.route_level,
                    "confidence": signal.confidence,
                },
            )

            # 3. 合并参数
            parsed_params = signal.entities.copy()
            # time_range 优先级: time_range > time_range_days > query 提取 > config 默认
            resolved_days = _resolve_time_range(request.time_range)
            time_range_days: int = (
                resolved_days
                or request.time_range_days
                or signal.time_range_days
                or self._settings.analysis.default_time_range_days
            )
            parsed_params["days"] = time_range_days

            # 4. 选择性补充上下文实体（非分析意图时跳过）
            if not is_recall:
                for key, val in relevant_entities.items():
                    if key not in parsed_params or not parsed_params[key]:
                        parsed_params[key] = val
                        _logger.info(
                            "entity '%s'='%s' inherited from session context", key, val
                        )

            # 4.5 实体关联补充：有 po_number 但缺 supplier_id 时从 DB 反查
            if not is_recall:
                await self._enrich_entities(parsed_params)

            # 5. 路由决策
            # - RECALL / DATA_LOOKUP / 低置信度 ANALYSIS → 强制 ReAct
            #   （回溯需要看短期记忆；事实查询直接调 query_* 工具；低置信度不
            #    宜走 DAG 模板分析以免输出离题报告）
            # - L1/L2 命中且非 COMPREHENSIVE → DAG 执行
            # - COMPREHENSIVE + 有具体实体 → 实体维度 DAG
            # - 其余 → ReAct 兜底
            from core.orchestrator.signal import IntentKind as _IntentKindRoute

            # TECH-DEBT(#8): DATA_LOOKUP 强制走 ReAct，缺少 Lookup DAG 模板
            is_data_lookup = signal.intent_kind == _IntentKindRoute.DATA_LOOKUP
            low_confidence = (
                signal.intent_kind == _IntentKindRoute.ANALYSIS
                and signal.route_level == 3
                and signal.confidence < self._settings.intent_routing.l3_dag_min_confidence
            )

            generic_template_key: str | None = None  # 通用模板键（批 5）

            if is_recall or is_data_lookup or low_confidence:
                use_dag = False
            else:
                has_entity = bool(
                    parsed_params.get("po_number")
                    or parsed_params.get("supplier_id")
                    or parsed_params.get("payment_number")
                    or parsed_params.get("invoice_number")
                    or parsed_params.get("receipt_number")
                )

                # 批 5：COMPREHENSIVE + 无实体 + 概览触发词 → 通用 DAG 模板
                if (
                    self._settings.intent_routing.generic_template_enabled
                    and analysis_type == AnalysisType.COMPREHENSIVE
                    and not has_entity
                    and _matches_overview_query(request.query)
                ):
                    generic_template_key = "recent_procurement_health"

                use_dag = (
                    generic_template_key is not None
                    or signal.route_level == 25  # L2.5 案例命中
                    or (signal.route_level in (1, 2) and analysis_type != AnalysisType.COMPREHENSIVE)
                    or (analysis_type == AnalysisType.COMPREHENSIVE and has_entity)
                )

            # ── output_mode 自适应解析 ──────────────────────────────────────────
            # 解析顺序：
            #   1) 显式 detailed/brief/table/chat → 严格尊重，不做任何覆盖
            #   2) auto（默认）→ 按 intent_kind 解析：
            #         data_lookup → chat（事实查询，自然简洁直答）
            #         其他       → detailed（保持原有报告体验）
            #   3) DAG 路径降级保护：解析后若是 chat，强制升 brief
            #         （ReportAgent prompt 主体是"报告生成器"，与 chat 冲突；
            #          这一保护对显式 chat 也生效，避免 DAG 路径行为漂移）
            from core.observability.tracing import record_span

            with record_span("orchestrator", "resolve_output_mode") as om_attrs:
                om_attrs["requested"] = request.output_mode
                effective_output_mode = request.output_mode
                if effective_output_mode == "auto":
                    if is_data_lookup:
                        effective_output_mode = "chat"
                        _logger.info("output_mode auto → 'chat' (intent=data_lookup)")
                    else:
                        effective_output_mode = "detailed"
                        _logger.info("output_mode auto → 'detailed' (default for non-lookup)")
                if use_dag and effective_output_mode == "chat":
                    _logger.info(
                        "output_mode 'chat' downgraded to 'brief' on DAG path "
                        "(ReportAgent requires structured output)"
                    )
                    effective_output_mode = "brief"

                output_mode_prompt = _build_output_mode_prompts(self._settings).get(
                    effective_output_mode, ""
                )
                om_attrs["resolved"] = effective_output_mode
                om_attrs["has_prompt"] = bool(output_mode_prompt)
                om_attrs["status"] = "ok"

            _logger.info(
                "route decision: path=%s analysis_type=%s days=%d entities=%s",
                "DAG" if use_dag else "agent",
                analysis_type.value,
                time_range_days,
                {k: v for k, v in parsed_params.items() if k != "days" and v},
            )

            # trace span: 标记执行路径（DAG / agent），便于命中率聚合
            from core.observability.tracing import record_span
            with record_span("orchestrator", "route_execution") as exec_attrs:
                exec_attrs["execution"] = "dag" if use_dag else "agent"
                exec_attrs["analysis_type"] = analysis_type.value
                exec_attrs["route_level"] = signal.route_level
                exec_attrs["confidence"] = signal.confidence
                exec_attrs["l3_threshold_used"] = self._settings.intent_routing.l3_dag_min_confidence

            stage_name = "dag_planned" if use_dag else "react_started"
            _publish_stage_safe(
                stage_name,
                {"analysis_type": analysis_type.value},
            )
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
                output_mode_prompt=output_mode_prompt,
                use_agent_fallback=not use_dag,
                context_summary=session_ctx.get("context_summary", ""),
                skip_memory_write=is_recall,
                generic_template_key=generic_template_key,
            )

            # 5. 持久化（非分析意图的回溯查询不写入长期记忆和报告，
            #    避免 "Q: 上次分析的结果呢 A: ..." 被存入记忆产生循环引用）
            if not is_recall:
                # 5a. 实体上下文持久化：合并路由阶段 + 执行结果中的实体
                await self._save_session_entities(
                    session_id, parsed_params, result,
                )
                # 5b. 报告持久化
                await asyncio.to_thread(self._persist_report, result)
            return result

        except Exception:
            raise

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
        output_mode_prompt: str = "",
        use_agent_fallback: bool = False,
        context_summary: str = "",
        skip_memory_write: bool = False,
        generic_template_key: str | None = None,
    ) -> AnalysisResult:
        """通过 DAG Executor 执行分析（统一入口）。

        当 use_agent_fallback=True 或 DAG 模板不可用时，构造单节点 agent DAG
        交由 DAGExecutor 执行，等价于原 _execute_react 的行为。
        """
        from core.orchestrator.dag.templates import load_dag_template, load_generic_template
        from core.orchestrator.dag.validator import DAGValidator

        is_agent_path = use_agent_fallback
        dag_tasks = None

        if not use_agent_fallback:
            # L2.5 案例命中 → 优先复用 dag_hint（跳过模板加载）
            if getattr(signal, "dag_hint", None):
                dag_tasks = signal.dag_hint
                _logger.info(
                    "L2.5 dag_hint reused: %d tasks, route_level=%d",
                    len(dag_tasks), signal.route_level,
                )
            elif generic_template_key:
                # 通用概览模板（批 5）
                dag_tasks = load_generic_template(generic_template_key, params)
                if dag_tasks is not None:
                    _logger.info(
                        "generic template loaded: key=%s tasks=%d",
                        generic_template_key, len(dag_tasks),
                    )
            else:
                dag_tasks = load_dag_template(analysis_type, params)
            if dag_tasks is None:
                _logger.info("no DAG template for %s, fallback to agent", analysis_type.value)
                is_agent_path = True

        if not is_agent_path and dag_tasks is not None:
            _executor = self._lazy_dag_executor
            validator = DAGValidator(_executor._registry)
            is_valid, error = validator.validate(dag_tasks)
            if not is_valid:
                _logger.warning("DAG validation failed: %s, fallback to agent", error)
                is_agent_path = True

        # agent 路径：构造单节点 agent DAG
        if is_agent_path:
            dag_tasks = [
                {
                    "task_id": "react",
                    "type": "agent",
                    "tool_name": "agent",
                    "inputs": {
                        "analysis_type": analysis_type,
                        "query": query,
                        "session_id": session_id,
                        "user_id": user_id,
                        "time_range_days": time_range_days,
                        "context_summary": context_summary,
                        "output_mode_prompt": output_mode_prompt,
                        "skip_memory_write": skip_memory_write,
                    },
                    "depends_on": [],
                    "timeout_sec": 900,
                    "output_key": "agent_result",
                },
            ]

        executor = self._lazy_dag_executor

        _logger.info(
            "executing DAG: type=%s tasks=%d route_level=%d confidence=%.3f path=%s",
            analysis_type.value,
            len(dag_tasks),
            signal.route_level,
            signal.confidence,
            "agent" if is_agent_path else "DAG",
        )

        # 记录 context_budget span（DAG 路径）
        if not is_agent_path:
            self._record_dag_context_budget(query=query)

        dag_result = await executor.execute(
            dag_tasks,
            output_mode_prompt=output_mode_prompt,
            agent=self._lazy_agent if is_agent_path else None,
            query=query,
            analysis_type=analysis_type.value,
        )
        duration_ms = (time.monotonic() - start_time) * 1000.0

        # ── agent 路径：从 agent_result dict 构造 AnalysisResult ──
        if is_agent_path:
            agent_result = dag_result.get("outputs", {}).get("agent_result")
            if isinstance(agent_result, dict) and agent_result:
                route_info: dict[str, Any] = {"route_type": "agent"}
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
                    report_markdown=_strip_think_tags(
                        agent_result.get("report_markdown", "")
                    ),
                    completed_tasks=agent_result.get("completed_tasks", []),
                    failed_tasks=agent_result.get("failed_tasks", []),
                    duration_ms=duration_ms,
                )
            # agent 执行失败
            failed_list = [
                f"{tid}: {err}"
                for tid, err in dag_result.get("failed_tasks", {}).items()
            ]
            return AnalysisResult(
                report_id=report_id,
                trace_id=trace_id,
                status=AnalysisStatus.FAILED,
                analysis_type=analysis_type,
                query=query,
                user_id=user_id,
                session_id=session_id,
                time_range=f"最近 {time_range_days} 天",
                failed_tasks=failed_list,
                error=ErrorInfo(
                    code="AGENT_EXECUTION_FAILED",
                    message="; ".join(failed_list[:3]) if failed_list else "agent returned no result",
                ),
                duration_ms=duration_ms,
            )

        # ── 标准 DAG 路径：工具 + 报告 ──
        report_text = dag_result.get("report", "")

        # 短期记忆写入
        if not skip_memory_write:
            await self._save_dag_to_short_term_memory(
                query=query,
                response=report_text,
                session_id=session_id,
                time_range_days=time_range_days,
            )

        # 长期记忆写入
        await self._save_dag_to_long_term_memory(
            user_id=user_id,
            session_id=session_id,
            query=query,
            response=report_text,
            analysis_type=analysis_type,
            time_range_days=time_range_days,
        )

        status = AnalysisStatus.SUCCESS
        if dag_result["status"] == "error":
            status = AnalysisStatus.FAILED
        elif dag_result["status"] == "warning":
            status = AnalysisStatus.PARTIAL_SUCCESS

        failed_list = [
            f"{tid}: {err}" for tid, err in dag_result.get("failed_tasks", {}).items()
        ]

        error_info: ErrorInfo | None = None
        report_err = dag_result.get("report_error")
        if report_err:
            status = AnalysisStatus.FAILED
            error_info = ErrorInfo(
                code=report_err["code"],
                message=report_err["message"],
            )
        elif status == AnalysisStatus.FAILED and failed_list:
            error_info = ErrorInfo(
                code="DAG_EXECUTION_FAILED",
                message="; ".join(failed_list[:3]),
            )

        return AnalysisResult(
            report_id=report_id,
            trace_id=trace_id,
            status=status,
            analysis_type=analysis_type,
            query=query,
            user_id=user_id,
            session_id=session_id,
            time_range=f"最近 {time_range_days} 天",
            report_markdown=_strip_think_tags(dag_result.get("report", "")),
            completed_tasks=dag_result.get("completed_tasks", []),
            failed_tasks=failed_list,
            error=error_info,
            summary={
                "route_type": "DAG",
                "route_level": signal.route_level,
                "route_confidence": signal.confidence,
                "route_reasoning": signal.reasoning,
                "dag_duration_sec": dag_result.get("duration_sec", 0),
            },
            duration_ms=duration_ms,
        )

    # ── DAG context_budget 记录 ────────────────────────────────────────

    def _record_dag_context_budget(
        self,
        *,
        query: str,
    ) -> None:
        """记录 DAG 路径的 context_budget span。

        DAG 路径的 LLM 调用仅发生在 ReportAgent，其 prompt 由
        报告模板 + 工具输出组成（不注入长期记忆）。
        此处记录查询的 token 估算，工具输出 token
        在执行前未知，由 ReportAgent 的 model span 覆盖。
        """
        from core.observability.tracing import estimate_tokens, record_span

        query_tokens = estimate_tokens(query)
        report_template_tokens = estimate_tokens("x" * 350)

        total_inject_tokens = report_template_tokens + query_tokens
        context_window = self._settings.llm.context_window
        budget_usage_pct = round(
            total_inject_tokens / context_window * 100, 1
        ) if context_window > 0 else 0.0

        with record_span(
            "context_budget", "context_budget_dag",
            route_type="DAG",
            report_template_tokens=report_template_tokens,
            long_term_memory_tokens=0,
            user_message_tokens=query_tokens,
            total_inject_tokens=total_inject_tokens,
            model_context_limit=context_window,
            budget_usage_pct=budget_usage_pct,
            note="不含工具输出 token（执行前未知，见 model span）",
        ):
            pass

    # ── DAG 长期记忆写入 ─────────────────────────────────────────────

    async def _save_dag_to_long_term_memory(
        self,
        user_id: str,
        session_id: str,
        query: str,
        response: str,
        analysis_type: AnalysisType,
        time_range_days: int,
    ) -> None:
        """将 DAG 分析结论写入长期记忆（memories 表）。

        与 P2PAgent 中的 save_memory 逻辑对齐：
        使用相同的 _build_memory_content / _build_memory_metadata 构建内容。
        写入失败不阻塞主流程。
        """
        if not response or not self._settings.memory.long_term_enabled:
            return

        try:
            from core.memory import get_long_term_memory

            ltm = get_long_term_memory()
            if self._provider is not None:
                content = self._provider.build_memory_content(
                    query=query, response=response, summary={},
                )
                metadata = self._provider.build_memory_metadata(
                    query=query,
                    analysis_type=analysis_type.value,
                    anomalies=[],
                    summary={},
                    time_range_days=time_range_days,
                )
            else:
                from modules.p2p.agent import _build_memory_content, _build_memory_metadata

                content = _build_memory_content(
                    query=query, response=response, summary={},
                )
                metadata = _build_memory_metadata(
                    query=query,
                    analysis_type=analysis_type.value,
                    anomalies=[],
                    summary={},
                    time_range_days=time_range_days,
                )
            await asyncio.to_thread(
                ltm.save_memory,
                user_id=user_id,
                session_id=session_id,
                memory_type="analysis_conclusion",
                content=content,
                metadata=metadata,
            )
            _logger.info(
                "DAG analysis conclusion saved to long-term memory: user=%s session=%s",
                user_id,
                session_id,
            )
        except Exception as exc:
            _logger.warning("DAG long-term memory save skipped: %s", exc)

    async def _save_dag_to_short_term_memory(
        self,
        query: str,
        response: str,
        session_id: str,
        time_range_days: int,
    ) -> None:
        """将 DAG 结果写入短期记忆（委托到 ShortTermMemory）。"""
        await self._short_term.save_dag_result(
            query=query,
            response=response,
            session_id=session_id,
            time_range_days=time_range_days,
            agent=self._lazy_agent,
        )

    # _execute_react 已删除——ReAct 场景通过 _execute_dag(use_agent_fallback=True) 统一走 DAG。
