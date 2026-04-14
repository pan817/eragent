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
from core.observability.middleware import publish_stage as _publish_stage
from core.orchestrator.router import IntentRouter

_logger = get_logger(__name__)


def _publish_stage_safe(name: str, attrs: dict[str, Any] | None = None) -> None:
    """orchestrator 内用的 stage 事件发布器：失败吞掉，不影响分析主流程。"""
    try:
        _publish_stage(name, attrs)
    except Exception:  # noqa: BLE001
        _logger.debug("publish_stage failed", exc_info=True)

# 输出模式 → prompt 后缀
_OUTPUT_MODE_PROMPTS: dict[str, str] = {
    "detailed": "",
    "brief": "请以简报摘要形式输出，控制在 3-5 个要点，突出关键数据和结论，总字数不超过 500 字。",
    "table": "请优先使用 Markdown 表格呈现核心数据，辅以不超过 2 句话的结论。",
}


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
    """P2P 分析编排器。

    协调意图解析、DAG/Agent 调度和结果封装的核心组件。
    Checkpointer（短期记忆）在 Orchestrator 级别管理，
    作为基础设施注入给 P2PAgent 和 DAG 路径共享使用。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        if settings is None:
            settings = get_settings()
        self._settings: Settings = settings
        self._agent: Any = None
        self._dag_executor: Any = None
        self._report_agent: Any = None
        self._checkpointer: Any | None = None
        self._checkpointer_cm: Any | None = None
        self._lock = threading.RLock()
        self._init_components()

    def _init_components(self) -> None:
        """初始化轻量级组件。"""
        self._intent_router: IntentRouter = IntentRouter(settings=self._settings)
        self._timing_middleware: TimingMiddleware = TimingMiddleware(
            agent_name="p2p_agent"
        )

    # ── Checkpointer 生命周期管理 ──────────────────────────────────

    def _get_checkpointer(self) -> Any:
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
            attach_checkpointer_tracing(saver, self._timing_middleware)
            self._checkpointer_cm = cm
            self._checkpointer = saver
            atexit.register(self._close_checkpointer)
            _logger.info("checkpointer initialized (Orchestrator-level)")
            return saver

    def _close_checkpointer(self) -> None:
        """关闭 checkpointer 的 context manager（atexit 回调，幂等）。"""
        cm = self._checkpointer_cm
        if cm is None:
            return
        self._checkpointer_cm = None
        self._checkpointer = None
        try:
            cm.__exit__(None, None, None)
        except Exception as exc:
            _logger.debug("checkpointer close failed (non-critical): %s", exc)

    def _ensure_checkpointer(self) -> Any | None:
        """获取 checkpointer，初始化失败时返回 None（不阻塞主流程）。"""
        try:
            return self._get_checkpointer()
        except Exception as exc:
            _logger.warning(
                "checkpointer init failed, short-term memory disabled: %s", exc
            )
            from core.observability.middleware import record_span

            with record_span(
                "checkpoint", "checkpointer_init_failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            ) as span_attrs:
                span_attrs["status"] = "error"
            return None

    def clear_short_term_memory(self, session_id: str | None = None) -> int:
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

    # ── 延迟初始化 ─────────────────────────────────────────────────

    @property
    def _lazy_agent(self) -> Any:
        """延迟初始化 P2PAgent 实例（Level 3 ReAct 兜底）。"""
        if self._agent is None:
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

    # ── 实体 DB 验证 ──────────────────────────────────────────────────

    async def _validate_entities(
        self, repo: Any, params: dict[str, Any]
    ) -> None:
        """验证正则提取的实体是否在 DB 中存在，不存在则清除。

        防止误匹配（如 "最近 10045 天" 中 10045 被当作 PO 号）。
        验证失败不阻塞主流程。
        """
        from core.observability.middleware import record_span

        with record_span("entity", "validate_entities") as span_attrs:
            input_entities = {k: v for k, v in params.items() if k != "days" and v}
            span_attrs["input"] = input_entities.copy()
            discarded: list[str] = []
            errors: list[str] = []

            # PO 号验证
            po = params.get("po_number", "")
            if po:
                try:
                    orders = await asyncio.to_thread(
                        repo.query_purchase_orders, po_number=po
                    )
                    if not orders:
                        _logger.info("entity validation: po_number=%s not found in DB, discarded", po)
                        del params["po_number"]
                        discarded.append(f"po_number={po}")
                except Exception as exc:
                    _logger.warning("entity validation (po_number) failed: %s", exc)
                    errors.append(f"po_number: {type(exc).__name__}: {exc}")

            # 供应商验证
            sid = params.get("supplier_id", "")
            if sid:
                try:
                    orders = await asyncio.to_thread(
                        repo.query_purchase_orders, supplier_id=sid
                    )
                    if not orders:
                        _logger.info("entity validation: supplier_id=%s not found in DB, discarded", sid)
                        del params["supplier_id"]
                        discarded.append(f"supplier_id={sid}")
                except Exception as exc:
                    _logger.warning("entity validation (supplier_id) failed: %s", exc)
                    errors.append(f"supplier_id: {type(exc).__name__}: {exc}")

            # 发票号验证
            inv = params.get("invoice_number", "")
            if inv:
                try:
                    invoices = await asyncio.to_thread(
                        repo.query_invoices, supplier_id="", status=""
                    )
                    if not any(i.get("invoice_number") == inv for i in invoices):
                        _logger.info("entity validation: invoice_number=%s not found in DB, discarded", inv)
                        del params["invoice_number"]
                        discarded.append(f"invoice_number={inv}")
                except Exception as exc:
                    _logger.warning("entity validation (invoice_number) failed: %s", exc)
                    errors.append(f"invoice_number: {type(exc).__name__}: {exc}")

            # 付款号验证
            pay = params.get("payment_number", "")
            if pay:
                try:
                    payments = await asyncio.to_thread(
                        repo.query_payments, payment_number=pay
                    )
                    if not payments:
                        _logger.info("entity validation: payment_number=%s not found in DB, discarded", pay)
                        del params["payment_number"]
                        discarded.append(f"payment_number={pay}")
                except Exception as exc:
                    _logger.warning("entity validation (payment_number) failed: %s", exc)
                    errors.append(f"payment_number: {type(exc).__name__}: {exc}")

            # 收货号验证
            rcv = params.get("receipt_number", "")
            if rcv:
                try:
                    receipts = await asyncio.to_thread(
                        repo.query_receipts, po_number="", supplier_id=""
                    )
                    if not any(
                        r.get("receipt_id") == rcv or r.get("gr_number") == rcv
                        for r in receipts
                    ):
                        _logger.info("entity validation: receipt_number=%s not found in DB, discarded", rcv)
                        del params["receipt_number"]
                        discarded.append(f"receipt_number={rcv}")
                except Exception as exc:
                    _logger.warning("entity validation (receipt_number) failed: %s", exc)
                    errors.append(f"receipt_number: {type(exc).__name__}: {exc}")

            validated = {k: v for k, v in params.items() if k != "days" and v}
            span_attrs["validated"] = validated
            span_attrs["discarded"] = discarded
            if errors:
                span_attrs["validation_errors"] = errors
                span_attrs["status"] = "warning"
            else:
                span_attrs["status"] = "ok"

    # ── 实体关联补充 ─────────────────────────────────────────────────

    async def _enrich_entities(self, params: dict[str, Any]) -> None:
        """验证并补充实体关联。

        两阶段处理：
        阶段 1：DB 验证——正则提取的实体在 DB 中是否存在，不存在则清除（防误匹配）。
        阶段 2：级联补充——从已有实体反查关联实体。
          1. payment_number → invoice_number（付款关联发票）
          2. receipt_number → po_number, supplier_id（收货关联 PO 和供应商）
          3. invoice_number → po_number, supplier_id（发票关联 PO 和供应商）
          4. po_number → supplier_id（PO 关联供应商）

        查询失败不阻塞主流程。
        """
        from core.observability.middleware import record_span

        try:
            from modules.p2p.tools import _get_repository
            repo = _get_repository()
        except Exception:
            return

        # ── 阶段 1：DB 验证 ──
        await self._validate_entities(repo, params)

        # ── 阶段 2：级联补充 ──
        with record_span("entity", "enrich_entities") as span_attrs:
            before = {k: v for k, v in params.items() if k != "days" and v}
            span_attrs["before"] = before.copy()
            enriched_pairs: list[str] = []
            errors: list[str] = []

            def _log_enriched(src: str, src_val: str, tgt: str, tgt_val: str) -> None:
                _logger.info("entity enriched: %s=%s → %s=%s", src, src_val, tgt, tgt_val)
                enriched_pairs.append(f"{src}={src_val}→{tgt}={tgt_val}")

            # 1. payment_number → invoice_number
            pay = params.get("payment_number", "")
            if pay and not params.get("invoice_number"):
                try:
                    payments = await asyncio.to_thread(
                        repo.query_payments, payment_number=pay
                    )
                    if payments:
                        inv_num = payments[0].get("invoice_number", "")
                        if inv_num:
                            params["invoice_number"] = inv_num
                            _log_enriched("payment_number", pay, "invoice_number", inv_num)
                except Exception as exc:
                    _logger.warning("entity enrichment (payment→invoice) failed: %s", exc)
                    errors.append(f"payment→invoice: {type(exc).__name__}: {exc}")

            # 2. receipt_number → po_number, supplier_id
            rcv = params.get("receipt_number", "")
            if rcv:
                try:
                    receipts = await asyncio.to_thread(
                        repo.query_receipts, po_number="", supplier_id=""
                    )
                    matched = [r for r in receipts if r.get("receipt_id") == rcv or r.get("gr_number") == rcv]
                    if matched:
                        if not params.get("po_number"):
                            po_num = matched[0].get("po_number", "")
                            if po_num:
                                params["po_number"] = po_num
                                _log_enriched("receipt_number", rcv, "po_number", po_num)
                        if not params.get("supplier_id"):
                            sid = matched[0].get("supplier_id", "")
                            if sid:
                                params["supplier_id"] = sid
                                _log_enriched("receipt_number", rcv, "supplier_id", sid)
                except Exception as exc:
                    _logger.warning("entity enrichment (receipt→po/supplier) failed: %s", exc)
                    errors.append(f"receipt→po/supplier: {type(exc).__name__}: {exc}")

            # 3. invoice_number → po_number, supplier_id
            inv = params.get("invoice_number", "")
            if inv:
                try:
                    invoices = await asyncio.to_thread(
                        repo.query_invoices, supplier_id="", status=""
                    )
                    matched = [i for i in invoices if i.get("invoice_number") == inv]
                    if matched:
                        if not params.get("po_number"):
                            po_num = matched[0].get("po_number", "")
                            if po_num:
                                params["po_number"] = po_num
                                _log_enriched("invoice_number", inv, "po_number", po_num)
                        if not params.get("supplier_id"):
                            sid = matched[0].get("supplier_id", "")
                            if sid:
                                params["supplier_id"] = sid
                                _log_enriched("invoice_number", inv, "supplier_id", sid)
                except Exception as exc:
                    _logger.warning("entity enrichment (invoice→po/supplier) failed: %s", exc)
                    errors.append(f"invoice→po/supplier: {type(exc).__name__}: {exc}")

            # 4. po_number → supplier_id
            po = params.get("po_number", "")
            if po and not params.get("supplier_id"):
                try:
                    orders = await asyncio.to_thread(
                        repo.query_purchase_orders, po_number=po
                    )
                    if orders:
                        sid = orders[0].get("supplier_id", "")
                        if sid:
                            params["supplier_id"] = sid
                            _log_enriched("po_number", po, "supplier_id", sid)
                except Exception as exc:
                    _logger.warning("entity enrichment (po→supplier) failed: %s", exc)
                    errors.append(f"po→supplier: {type(exc).__name__}: {exc}")

            after = {k: v for k, v in params.items() if k != "days" and v}
            span_attrs["after"] = after
            span_attrs["enriched"] = enriched_pairs
            if errors:
                span_attrs["enrichment_errors"] = errors
                span_attrs["status"] = "warning"
            else:
                span_attrs["status"] = "ok"

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
                checkpointer = self._ensure_checkpointer()
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

        # 无指代词时不做隐式继承——避免用户的新查询被静默限定到历史实体范围。
        # 例如用户上一轮分析了 SUP-001，这一轮问"分析价格差异"，
        # 不应自动限定为 SUP-001 的价格差异。

        return enhanced, relevant

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

        try:
            return await asyncio.wait_for(
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
            from core.observability.middleware import format_error_chain

            trace_status = "error"
            trace_error = format_error_chain(exc)
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
            analysis_type: AnalysisType = request.analysis_type or self._intent_router.resolve_type(signal)
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
                        _logger.debug(
                            "entity '%s'='%s' inherited from session context", key, val
                        )

            # 4.5 实体关联补充：有 po_number 但缺 supplier_id 时从 DB 反查
            if not is_recall:
                await self._enrich_entities(parsed_params)

            # 5. 路由决策
            # - 非分析意图 → 强制 ReAct（用户在回溯/闲聊，不是发起新分析）
            # - L1/L2 命中且非 COMPREHENSIVE → DAG 执行
            # - COMPREHENSIVE + 有具体实体 → 实体维度 DAG
            # - 其余 → ReAct 兜底
            if is_recall:
                use_dag = False
            else:
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

            output_mode_prompt = _OUTPUT_MODE_PROMPTS.get(request.output_mode, "")

            if use_dag:
                _publish_stage_safe(
                    "dag_planned",
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
                )
            else:
                _publish_stage_safe(
                    "react_started",
                    {"analysis_type": analysis_type.value},
                )
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
                    context_summary=session_ctx.get("context_summary", ""),
                    skip_memory_write=is_recall,
                    output_mode_prompt=output_mode_prompt,
                )

            # 5. 持久化（非分析意图的回溯查询不写入长期记忆和报告，
            #    避免 "Q: 上次分析的结果呢 A: ..." 被存入记忆产生循环引用）
            if not is_recall:
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

        # 记录 context_budget span（DAG 路径）
        self._record_dag_context_budget(query=query)

        dag_result = await executor.execute(
            dag_tasks,
            output_mode_prompt=output_mode_prompt,
        )
        duration_ms = (time.monotonic() - start_time) * 1000.0

        report_text = dag_result.get("report", "")

        # 短期记忆写入
        await self._save_dag_to_short_term_memory(
            query=query,
            response=report_text,
            session_id=session_id,
            time_range_days=time_range_days,
        )

        # 长期记忆写入（save_memory）
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

        # 报告生成失败是致命错误：没有 markdown 报告的分析结果对用户无意义
        # 即便其他工具成功（status=warning），也升级为 FAILED
        error_info: ErrorInfo | None = None
        report_err = dag_result.get("report_error")
        if report_err:
            status = AnalysisStatus.FAILED
            error_info = ErrorInfo(
                code=report_err["code"],
                message=report_err["message"],
            )
        elif status == AnalysisStatus.FAILED and failed_list:
            # 全部工具失败（无 report_error 但 dag_status=error）
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
            report_markdown=dag_result.get("report", ""),
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
        from core.observability.middleware import estimate_tokens, record_span

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
            from modules.p2p.agent import _build_memory_content, _build_memory_metadata

            ltm = get_long_term_memory()
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
            _logger.debug(
                "DAG analysis conclusion saved to long-term memory: user=%s session=%s",
                user_id,
                session_id,
            )
        except Exception as exc:
            _logger.warning("DAG long-term memory save skipped: %s", exc)

    # ── DAG 短期记忆写入 ────────────────────────────────────────────

    async def _save_dag_to_short_term_memory(
        self,
        query: str,
        response: str,
        session_id: str,
        time_range_days: int,
    ) -> None:
        """将 DAG 执行的 query + response 写入 checkpointer 短期记忆。

        通过 agent.update_state 写入，确保 checkpoint 的 channel 格式
        与 LangGraph agent 内部一致（避免手动 put 导致格式不兼容）。
        写入失败不阻塞主流程。
        """
        from core.observability.middleware import record_span

        with record_span("checkpoint", "dag_short_term_write") as span_attrs:
            span_attrs["session_id"] = session_id
            span_attrs["query_length"] = len(query)
            span_attrs["response_length"] = len(response)

            try:
                agent = self._lazy_agent

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

                # update_state 以 LangGraph 内部格式写入 checkpoint，
                # 与 agent.ainvoke 自动写入的格式完全一致。
                await asyncio.to_thread(
                    agent_graph.update_state,
                    config,
                    {"messages": [user_msg, ai_msg]},
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
        context_summary: str = "",
        skip_memory_write: bool = False,
        output_mode_prompt: str = "",
    ) -> AnalysisResult:
        """通过 P2PAgent ReAct 模式执行分析（Level 3 兜底）。"""
        agent_result: dict[str, Any] = await self._lazy_agent.run(
            analysis_type=analysis_type,
            query=query,
            params=params,
            time_range_days=time_range_days,
            user_id=user_id,
            session_id=session_id,
            context_summary=context_summary,
            skip_memory_write=skip_memory_write,
            output_mode_prompt=output_mode_prompt,
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
