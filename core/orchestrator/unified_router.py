"""统一 LLM 路由器。

合并意图分类 + 参数提取 + 指代消解 + 跨实体判断为一次 LLM 调用。
替代原有 L1 分析匹配 + L2 向量查询 + L3 LLM 分类 + ParamExtractor。
"""

from __future__ import annotations

import json
from typing import Any

from config.settings import Settings
from core.logging_utils import get_logger
from core.orchestrator.signal import IntentKind, QuerySignal

_logger = get_logger(__name__)

# order_by 合法枚举值
_VALID_ORDER_BY = frozenset({"date_desc", "date_asc", "amount_desc", "amount_asc"})

# 实体字段白名单
_ENTITY_FIELDS = frozenset({
    "po_number", "supplier_id", "invoice_number",
    "payment_number", "receipt_number",
})

# intent_kind 合法枚举值（clarification 已废弃，不再作为 LLM 输出选项）
_VALID_INTENT_KINDS = frozenset({
    "analysis", "data_lookup",
    "meta", "chitchat", "out_of_scope",
})

_UNIFIED_PROMPT = """\
你是 ERP 采购分析系统的统一意图解析器。一次性完成：意图分类 + 参数提取 + 指代消解 + 跨实体判断。

当前日期：{current_date}（{timezone}）。
{role_section}
## 会话实体上下文（上一轮涉及的实体，用于指代消解）
{session_entities}

## intent_kind 枚举
- analysis: 异常检测/合规检查/绩效评估等分析工作（落入下面 analysis_type 之一）。即使缺少时间/实体参数也判 analysis，系统有默认参数。意图模糊但涉及采购业务时也归此类，type 填 comprehensive。
- data_lookup: 纯事实查询/单据检索（"查最新PO"/"列出发票"），不涉及异常或评估
- meta: 系统能力询问（"你支持哪些分析"）
- chitchat: 闲聊/问候/非业务
- out_of_scope: 明确指向非 P2P 模块（销售/HR/生产）

## analysis_type 枚举（仅 intent_kind=analysis 时填，否则空串）
{analysis_types_section}

## 判定规则
1. 核心原则：尽量判为 analysis 或 data_lookup 让系统执行，**绝不拒绝用户**。
2. "查/查询/查看/列出/最新" + 业务实体 → 优先 data_lookup。
3. 缺少时间/实体参数不影响判定，系统有默认参数可执行。
4. analysis_type 选最具体的；实在无法确定具体类型时填 comprehensive，让系统自行分析。
5. **禁止返回 clarification**——任何涉及采购业务的查询都归 analysis 或 data_lookup。

## 跨实体判断（is_cross_entity）
当查询涉及从一种实体跳转到另一种时为 true：
- "这个付款单对应的PO" → true（付款→采购订单）
- "这个供应商的发票" → true（供应商→发票）
- "查最新的PO" → false（单实体查询）
- "分析SUP-001的三路匹配" → false（分析，非跨实体检索）

## 指代消解
结合"会话实体上下文"，将"这个/该/上次的"替换为具体实体ID，写入 resolved_query。
- 上下文有 payment_number=PAY-001，用户说"这个支付单的PO" → resolved_query="支付单 PAY-001 对应的采购订单"
- 无法消解时 resolved_query 与原 query 相同。

## 参数提取
- 实体编号：PO-xxxx / SUP-xxxx / INV-xxxx / PAY-xxxx / RCV-xxxx，明确出现才填，代词填 null
- days: 时间范围天数（"最近7天"→7，"本月"→当月天数），未指定填 null
- limit: 数量限制（"一个"→1，"前5条"→5），未指定填 null
- order_by: date_desc / date_asc / amount_desc / amount_asc，未指定填 null

## confidence 锚点
- >0.9: 核心名词直接命中，无歧义
- 0.7-0.9: 语义强相关需推断
- 0.5-0.7: 多类共存或模糊
- <0.5: 极度模糊

## 输出格式（纯 JSON，不要 markdown 代码块，第一个字符必须是 {{）
{{"intent_kind":"<枚举值>","type":"<analysis_type或空串>","confidence":0.0,"is_cross_entity":false,"resolved_query":"<消解后查询>","missing_params":[],"po_number":null,"supplier_id":null,"invoice_number":null,"payment_number":null,"receipt_number":null,"days":null,"limit":null,"order_by":null}}

## 示例
- "查询最新的一个PO" → {{"intent_kind":"data_lookup","type":"","confidence":0.9,"is_cross_entity":false,"resolved_query":"查询最新的一个PO","missing_params":[],"po_number":null,"supplier_id":null,"invoice_number":null,"payment_number":null,"receipt_number":null,"days":null,"limit":1,"order_by":"date_desc"}}
- "这个支付单对应的PO"（上下文: payment_number=PAY-001）→ {{"intent_kind":"data_lookup","type":"","confidence":0.85,"is_cross_entity":true,"resolved_query":"支付单 PAY-001 对应的采购订单","missing_params":[],"po_number":null,"supplier_id":null,"invoice_number":null,"payment_number":"PAY-001","receipt_number":null,"days":null,"limit":null,"order_by":null}}
- "分析最近30天SUP-001的价格差异" → {{"intent_kind":"analysis","type":"price_variance","confidence":0.95,"is_cross_entity":false,"resolved_query":"分析最近30天SUP-001的价格差异","missing_params":[],"po_number":null,"supplier_id":"SUP-001","invoice_number":null,"payment_number":null,"receipt_number":null,"days":30,"limit":null,"order_by":null}}
- "做一下三路匹配" → {{"intent_kind":"analysis","type":"three_way_match","confidence":0.85,"is_cross_entity":false,"resolved_query":"做一下三路匹配","missing_params":[],"po_number":null,"supplier_id":null,"invoice_number":null,"payment_number":null,"receipt_number":null,"days":null,"limit":null,"order_by":null}}
- "有没有该付钱还没付的账单" → {{"intent_kind":"analysis","type":"payment_compliance","confidence":0.85,"is_cross_entity":false,"resolved_query":"有没有该付钱还没付的账单","missing_params":[],"po_number":null,"supplier_id":null,"invoice_number":null,"payment_number":null,"receipt_number":null,"days":null,"limit":null,"order_by":null}}

用户查询：{query}"""


def _parse_unified_response(content: str, raw_query: str) -> QuerySignal:
    """解析统一 LLM 的 JSON 响应为 QuerySignal。

    解析失败时返回 ANALYSIS/COMPREHENSIVE 兜底信号。
    """
    raw = content.strip()
    # 清理 markdown 代码块
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _logger.warning("unified router: JSON parse failed: %s", content[:200])
        return _fallback_signal(raw_query)

    if not isinstance(data, dict):
        return _fallback_signal(raw_query)

    # intent_kind（clarification 已从合法枚举移除，LLM 若仍返回则降级为 analysis）
    ik_raw = data.get("intent_kind", "analysis")
    if ik_raw not in _VALID_INTENT_KINDS:
        _logger.info(
            "intent_kind '%s' not in valid set, downgrade to analysis: query='%s'",
            ik_raw, raw_query[:80],
        )
        ik_raw = "analysis"
        # 降级时若 LLM 未给出具体 type，兜底 comprehensive
        if not (data.get("type") or ""):
            data["type"] = "comprehensive"

    # analysis_type
    analysis_type = data.get("type", "") or ""

    intent_kind = IntentKind(ik_raw)

    # confidence
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5

    # entities
    entities: dict[str, Any] = {}
    for field in _ENTITY_FIELDS:
        val = data.get(field)
        if val and isinstance(val, str):
            entities[field] = val.strip()

    # days
    days_raw = data.get("days")
    time_range_days: int | None = None
    if days_raw is not None:
        try:
            d = int(days_raw)
            if d > 0:
                time_range_days = d
        except (TypeError, ValueError):
            pass

    # limit / order_by → 放入 entities 供 lookup 使用
    limit_raw = data.get("limit")
    if limit_raw is not None:
        try:
            lim = int(limit_raw)
            if lim > 0:
                entities["limit"] = lim
        except (TypeError, ValueError):
            pass

    order_by = data.get("order_by")
    if order_by and isinstance(order_by, str) and order_by in _VALID_ORDER_BY:
        entities["order_by"] = order_by

    # is_cross_entity
    is_cross = bool(data.get("is_cross_entity", False))

    # resolved_query
    resolved = data.get("resolved_query", "") or ""

    # missing_params
    missing = data.get("missing_params", [])
    if not isinstance(missing, list):
        missing = []

    # keywords: 与现有约定兼容（keywords[0] = analysis_type 或 sentinel）
    if intent_kind == IntentKind.ANALYSIS and analysis_type:
        keywords = [analysis_type]
    elif intent_kind == IntentKind.DATA_LOOKUP:
        keywords = ["data_lookup"]
    elif intent_kind == IntentKind.CLARIFICATION:
        # clarification 已废弃，不应到达此处；防御性兜底为 comprehensive
        keywords = ["comprehensive"]
    elif intent_kind == IntentKind.META:
        keywords = ["meta"]
    elif intent_kind == IntentKind.CHITCHAT:
        keywords = ["chitchat"]
    elif intent_kind == IntentKind.OUT_OF_SCOPE:
        keywords = ["out_of_scope"]
    else:
        keywords = [analysis_type] if analysis_type else ["comprehensive"]

    return QuerySignal(
        raw_query=raw_query,
        intent_kind=intent_kind,
        keywords=keywords,
        entities=entities,
        missing_params=missing,
        time_range_days=time_range_days,
        route_level=3,  # 统一 LLM 视为 level 3
        confidence=confidence,
        reasoning=f"unified LLM: {ik_raw}/{analysis_type} conf={confidence:.2f}",
        is_cross_entity=is_cross,
        resolved_query=resolved,
    )


def _fallback_signal(raw_query: str) -> QuerySignal:
    """LLM 解析失败时的兜底信号。"""
    return QuerySignal(
        raw_query=raw_query,
        intent_kind=IntentKind.ANALYSIS,
        keywords=["comprehensive"],
        route_level=3,
        confidence=0.3,
        reasoning="unified LLM parse failed, fallback to comprehensive",
    )


class UnifiedRouter:
    """统一 LLM 路由器。

    一次 LLM 调用完成意图分类 + 参数提取 + 指代消解 + 跨实体判断。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._llm: Any = None

    def _ensure_llm(self) -> Any:
        """延迟初始化 llm_fast（关闭 thinking）。"""
        if self._llm is not None:
            return self._llm
        from core.llm.model_factory import build_chat_model

        self._llm = build_chat_model(self._settings.llm_fast, disable_thinking=True)
        return self._llm

    def route(
        self,
        query: str,
        session_entities: dict[str, Any] | None = None,
        analyst_role: str = "general",
    ) -> QuerySignal:
        """统一 LLM 路由：一次调用完成分类 + 参数提取。

        Args:
            query: 用户原始查询。
            session_entities: 上一轮涉及的实体（用于指代消解）。
            analyst_role: 分析师角色偏好。

        Returns:
            完整的 QuerySignal。
        """
        from core.observability.tracing import _safe_jsonable, estimate_tokens, record_span
        from core.time_utils import get_timezone_name, now_cn
        from modules.p2p.intent_rules import (
            ANALYSIS_TYPE_DESCRIPTIONS,
            ROLE_DESCRIPTIONS,
        )

        try:
            llm = self._ensure_llm()

            # 构建 prompt
            types_section = "\n".join(
                f"- {name}: {desc}" for name, desc in ANALYSIS_TYPE_DESCRIPTIONS
            )
            role_desc = ROLE_DESCRIPTIONS.get(analyst_role, "")
            role_section = f"\n分析师角色偏好：{role_desc}\n" if role_desc else ""

            entities_str = json.dumps(
                session_entities or {}, ensure_ascii=False,
            )

            prompt = _UNIFIED_PROMPT.format(
                query=query,
                role_section=role_section,
                analysis_types_section=types_section,
                current_date=now_cn().strftime("%Y-%m-%d"),
                timezone=get_timezone_name(),
                session_entities=entities_str,
            )

            model_name = (
                getattr(llm, "model_name", None)
                or getattr(llm, "model", "unknown")
            )

            with record_span("model", str(model_name)) as attrs:
                attrs["model"] = str(model_name)
                attrs["estimated_input_tokens"] = estimate_tokens(prompt)

                response = llm.invoke(prompt)
                content: str = (
                    response.content if hasattr(response, "content") else str(response)
                )

                usage = (
                    getattr(response, "usage_metadata", None)
                    or getattr(response, "response_metadata", None)
                )
                attrs["output"] = {
                    "content": content[:2000],
                    "usage": _safe_jsonable(usage) if usage else None,
                }

            signal = _parse_unified_response(content, query)

            _logger.info(
                "unified route: intent=%s type=%s conf=%.2f cross_entity=%s "
                "resolved='%s' entities=%s",
                signal.intent_kind.value,
                signal.keywords[0] if signal.keywords else "",
                signal.confidence,
                signal.is_cross_entity,
                signal.resolved_query[:80] if signal.resolved_query else "",
                {k: v for k, v in signal.entities.items() if v},
            )
            return signal

        except Exception as exc:
            _logger.warning("unified LLM route failed: %s", exc)
            return _fallback_signal(query)
