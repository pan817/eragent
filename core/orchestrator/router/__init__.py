"""意图路由器。

L0 bypass（正则匹配，零 LLM）+ UnifiedRouter（一次 LLM 调用完成
意图分类 + 参数提取 + 指代消解 + 跨实体判断）。

输出仍为 (AnalysisType, params)，与原 IntentParser 接口兼容，
Orchestrator 无需感知内部路由层级。
"""

from __future__ import annotations

import re
from typing import Any

from api.schemas.domain import AnalysisType
from config.settings import Settings, get_settings
from core.logging_utils import get_logger
from core.orchestrator.signal import IntentKind, QuerySignal

# sentinel 字符串（写入 ``signal.keywords[0]``，与 AnalysisType
# 枚举共用同一字段，便于 trace 展示与 orchestrator 分支判断）。
_KW_DATA_LOOKUP = "data_lookup"
_KW_META = "meta"
_KW_CHITCHAT = "chitchat"
_KW_OUT_OF_SCOPE = "out_of_scope"
_KW_RECALL = "recall"

# bypass 默认置信度
_BYPASS_CONFIDENCE = 0.95

_logger = get_logger(__name__)

# ── P2P 规则常量（从 modules/p2p/intent_rules.py 导入） ──────────────
from modules.p2p.intent_rules import (
    ANALYSIS_KEYWORDS as _ANALYSIS_KEYWORDS,
)


# ── 前置 bypass 检测（按 intent_kind 分类，跳过 L1/L2 直达对应分支） ──
#
# 设计原则：bypass 必须能区分 *为什么* 跳过——闲聊（CHITCHAT）和系统能力
# 询问（META）下游处理完全不同：前者只是友好拒答，后者要返回系统能力清单。
# 老版本把两者混在 ``_DIRECT_BYPASS_PATTERNS`` 一个布尔判断里，导致
# orchestrator 拿到 "你能做什么" 也只能输出"不属于 ERP 采购分析范围"，
# 这是用户体验的回归。

# RECALL：明确回溯历史会话内容
_RECALL_PATTERNS: list[re.Pattern[str]] = [
    # 时间指代回溯（"之前/刚才/前面"等既可能是回溯也可能是时间范围）
    re.compile(
        r"上次|上一次|刚才|刚刚|方才|最后一次|上回|earlier|last time",
        re.IGNORECASE,
    ),
    # 结果/内容回溯
    re.compile(
        r"结果呢|说的什么|分析了什么|做了什么|讲了什么|提到的|得出的结论|"
        r"结论是什么|报告呢|看看结果|查看结果|显示结果",
    ),
    # 重复/总结请求
    re.compile(
        r"再说一遍|重复一下|总结一下|回顾一下|概括一下|复述|"
        r"再讲一遍|重新说|帮我回忆",
    ),
]

# 含歧义的回溯词（需要排除"时间范围"用法后才判为回溯）
_AMBIGUOUS_RECALL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"之前|前面|previous", re.IGNORECASE),
]

# CHITCHAT：闲聊/问候/否定/纯确认（语义上明确"非业务"）
_CHITCHAT_PATTERNS: list[re.Pattern[str]] = [
    # 问候/感谢/告别
    re.compile(
        r"^(你好|您好|hello|hi|hey|谢谢|感谢|thanks|thank you|再见|拜拜|bye)\s*[!！。.]*$",
        re.IGNORECASE,
    ),
    # 否定/取消
    re.compile(
        r"^(不需要了|算了|取消|不用了|没事了|好的|知道了|明白了|ok|okay)\s*[。.!！]*$",
        re.IGNORECASE,
    ),
    # 纯确认/追问（无实质分析内容）
    re.compile(r"^(是的|对|嗯|好|可以|继续|然后呢|接下来呢|还有呢|详细说说)\s*[？?。.!！]*$"),
]

# META：系统能力 / 数据元信息询问（应由模板答系统支持范围，不是拒答）
_META_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"你能做什么|你会什么|有什么功能|怎么用|帮助|^help\b|"
        r"支持哪些|可以做什么|有哪些分析|有哪些功能|如何使用|怎么使用",
        re.IGNORECASE,
    ),
]

# _ANALYSIS_KEYWORDS, _LOOKUP_VERBS, _LOOKUP_ENTITIES, _LOOKUP_MODIFIERS
# 已从 modules.p2p.intent_rules 导入（见上方 import 块）

# L2 length-ratio 阈值已迁到 IntentRoutingSettings（通过 self._settings 注入）


def _has_analysis_keywords(query: str) -> bool:
    """检查 query 中是否包含分析关键词。"""
    q_lower = query.lower()
    return any(kw in q_lower for kw in _ANALYSIS_KEYWORDS)


# 强分析意图关键词：特定于某个 analysis_type 的核心术语。
# 与 _ANALYSIS_KEYWORDS（含"分析""查询"等泛用词）的区别在于：
# 这些词出现在查询中时，几乎确定用户要做一次新的分析，而非回溯历史。
_STRONG_ANALYSIS_INTENT_KEYWORDS: set[str] = {
    # 分析类型核心名词
    "三路匹配", "三单", "three way", "three-way", "3way",
    "价格差异", "ppv", "价差", "溢价",
    "付款合规", "逾期", "overdue", "拖欠", "应付未付",
    "供应商绩效", "kpi", "scorecard", "otif", "准时交货",
    "支出分析", "spend", "支出分布", "品类支出",
    "收货异常", "超量", "拒收", "退货", "入库异常",
    "重复发票", "duplicate", "重复开票", "一票两付",
    "折扣利用", "现金折扣", "折扣损失",
    "周期", "cycle", "lead time", "处理时间",
    "集中度", "concentration", "单一来源", "垄断",
    # 明确的新分析动作词（排除"分析"本身，因为"上次分析的结果"不是新分析意图）
    "检查一下", "帮我分析", "做一下", "再分析", "重新分析", "再做",
}


def _has_strong_analysis_intent(query: str) -> bool:
    """检查 query 是否包含强分析意图关键词。

    用于 RECALL bypass 的二次确认：含明确回溯词 + 强分析意图时，
    不走 bypass 让 L1/L2/L3 正常路由。
    """
    q_lower = query.lower()
    return any(kw in q_lower for kw in _STRONG_ANALYSIS_INTENT_KEYWORDS)


def _classify_bypass(query: str) -> IntentKind | None:
    """前置 bypass 分类——若命中则返回对应 ``IntentKind``，否则返回 None。

    与老版本 ``_is_non_analysis_query`` 的差异：返回**为什么 bypass**，
    而不是简单的"是否 bypass"。orchestrator 据此选择不同下游处理：

    - META → 模板答系统能力
    - CHITCHAT → 友好拒答
    - RECALL → 走 ReAct 让 Agent 看短期记忆

    优先级：CHITCHAT > META > RECALL（短问候比能力询问更具排他性）。

    判定规则：
    1. CHITCHAT 严格匹配（``^...$``）：避免把 "你好，分析一下三路匹配" 这种
       开头问候 + 真实意图的查询误判为闲聊。
    2. META 子串匹配（"你能做什么 / 支持哪些"）。
    3. 明确回溯（"上次 / 刚才 / 结果呢"）→ 仅当不含分析关键词时判 RECALL，
       避免误吃 "上次那个供应商的三路匹配再分析一下" 中的回溯前缀。
    4. 歧义回溯（"之前 / previous"）→ 仅当不含分析关键词时判 RECALL，
       避免误吃 "分析之前 30 天的价格差异" 中的时间修饰用法。
    """
    q = query.strip()

    for pattern in _CHITCHAT_PATTERNS:
        if pattern.search(q):
            return IntentKind.CHITCHAT

    for pattern in _META_PATTERNS:
        if pattern.search(q):
            return IntentKind.META

    # RECALL 与歧义 RECALL 统一处理：含强分析意图关键词时不 bypass，
    # 让 L1/L2/L3 正常路由以保留分析意图。
    # 使用 _has_strong_analysis_intent 而非 _has_analysis_keywords，
    # 避免"分析"等泛用词让真正的回溯查询（"上次分析的结果呢"）逃逸。
    for pattern in _RECALL_PATTERNS:
        if pattern.search(q):
            if not _has_strong_analysis_intent(q):
                return IntentKind.RECALL
            return None

    for pattern in _AMBIGUOUS_RECALL_PATTERNS:
        if pattern.search(q):
            if not _has_analysis_keywords(q):
                return IntentKind.RECALL
            return None

    return None


def _is_non_analysis_query(query: str) -> bool:
    """旧接口兼容：是否非分析意图（会话回溯/闲聊/META 等）。

    保留为薄壳，内部委托给 :func:`_classify_bypass`。orchestrator 中仍有
    历史调用点（指代消解、长期记忆写入跳过等）依赖它做布尔判断。
    """
    return _classify_bypass(query) is not None




# ── 参数提取 ─────────────────────────────────────────────────────────

def _extract_params(
    query: str,
    entity_patterns: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """从查询文本中用正则提取业务参数。

    正则模式从 config.yaml 的 analysis.entity_patterns 读取，
    支持每个客户按自己的 EBS 编号格式配置。
    每个实体类型支持多个正则（按顺序尝试，首个匹配即返回）。

    Args:
        query: 用户查询文本。
        entity_patterns: 实体正则配置，为 None 时使用默认模式。
    """
    if entity_patterns is None:
        entity_patterns = _DEFAULT_ENTITY_PATTERNS

    params: dict[str, Any] = {}

    for entity_name, patterns in entity_patterns.items():
        for pat_str in patterns:
            try:
                match = re.search(pat_str, query, re.IGNORECASE)
            except re.error:
                _logger.warning("invalid entity pattern for %s: %s", entity_name, pat_str)
                continue
            if match:
                if entity_name == "days":
                    # days 用捕获组提取数字
                    params["days"] = int(match.group(1))
                else:
                    params[entity_name] = match.group()
                break  # 首个匹配即返回

    return params


# 默认实体正则（与 AnalysisSettings.entity_patterns 一致）
_DEFAULT_ENTITY_PATTERNS: dict[str, list[str]] = {
    "po_number": [r"PO-\d[\da-zA-Z_-]*\d", r"PO-\d+"],
    "supplier_id": [r"SUP-\d+"],
    "invoice_number": [r"INV-\d[\da-zA-Z_-]*\d", r"INV-\d+"],
    "payment_number": [r"PAY-\d[\da-zA-Z_-]*\d", r"PAY-\d+"],
    "receipt_number": [r"RCV-\d[\da-zA-Z_-]*\d", r"RCV-\d+", r"GR-\d+"],
    "days": [r"(?:最近|过去|近)\s*(\d+)\s*天", r"(?:past|last|recent)\s+(\d+)\s*days?"],
}


class IntentRouter:
    """意图路由器。

    L0 bypass + UnifiedRouter，提供 parse() 方法保持接口兼容。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings: Settings = settings or get_settings()
        self._case_store: Any = None  # 延迟注入的 DAGCaseStore
        self._unified_router: Any = None  # 统一 LLM 路由器（延迟初始化）
        _ir = self._settings.intent_routing
        _logger.info(
            "intent_routing settings: l3_dag_min=%.2f generic_tpl=%s",
            _ir.l3_dag_min_confidence,
            _ir.generic_template_enabled,
        )

    def set_case_store(self, case_store: Any) -> None:
        """注入 DAGCaseStore 实例（由 Orchestrator 在启动时调用）。"""
        self._case_store = case_store

    # ── 公开接口 ───────────────────────────────────────────────────

    def route(
        self,
        query: str,
        analyst_role: str = "general",
        session_entities: dict[str, Any] | None = None,
    ) -> QuerySignal:
        """路由查询，返回完整的 QuerySignal（供 Orchestrator DAG 分支使用）。"""
        signal = self._route(query, analyst_role=analyst_role, session_entities=session_entities)
        _logger.info(
            "intent routed: level=%d type=%s confidence=%.3f reason=%s",
            signal.route_level,
            signal.keywords[0] if signal.keywords else "unknown",
            signal.confidence,
            signal.reasoning,
        )
        return signal

    def resolve_type(self, signal: QuerySignal) -> AnalysisType:
        """从 QuerySignal 还原 AnalysisType。"""
        return self._resolve_type_from_signal(signal)

    def parse(self, query: str) -> tuple[AnalysisType, dict[str, Any]]:
        """解析自然语言查询，返回分析类型和参数。

        接口签名与原 IntentParser.parse() 完全一致，保持向后兼容。
        """
        signal = self.route(query)
        params = signal.entities.copy()
        if signal.time_range_days is not None:
            params["days"] = signal.time_range_days

        analysis_type = self._resolve_type_from_signal(signal)
        return analysis_type, params

    # ── 核心路由 ─────────────────────────────────────────────────────

    def _route(
        self,
        query: str,
        analyst_role: str = "general",
        session_entities: dict[str, Any] | None = None,
    ) -> QuerySignal:
        """路由主逻辑：L0 bypass + 统一 LLM 调用。"""
        from core.observability.tracing import record_span

        params = _extract_params(query, self._settings.analysis.entity_patterns)
        trace_data: dict[str, Any] = {
            "query": query,
            "params_regex": params.copy(),
        }

        with record_span("intent", "route_decision") as attrs:
            # L0 bypass：CHITCHAT / META / RECALL 跳过 LLM
            bypass_kind = _classify_bypass(query)
            trace_data["bypass"] = bypass_kind is not None
            trace_data["bypass_kind"] = bypass_kind.value if bypass_kind else None

            if bypass_kind is not None:
                signal = self._build_bypass_signal(
                    query=query,
                    intent_kind=bypass_kind,
                    params=params,
                )
                trace_data["hit_level"] = 0
                trace_data["result_type"] = signal.keywords[0] if signal.keywords else ""
                trace_data["confidence"] = signal.confidence
                trace_data["reasoning"] = signal.reasoning
                attrs.update(trace_data)
                return signal

            # 统一 LLM 调用（替代 L1/L2/L3 + ParamExtractor）
            if self._unified_router is None:
                from core.orchestrator.unified_router import UnifiedRouter
                self._unified_router = UnifiedRouter(settings=self._settings)

            signal = self._unified_router.route(
                query=query,
                session_entities=session_entities,
                analyst_role=analyst_role,
            )

            # 合并 regex 提取的实体（LLM 未提取到时用 regex 兜底）
            for k, v in params.items():
                if k != "days" and v and k not in signal.entities:
                    signal.entities[k] = v

            trace_data["hit_level"] = signal.route_level
            trace_data["result_type"] = signal.keywords[0] if signal.keywords else ""
            trace_data["confidence"] = signal.confidence
            trace_data["reasoning"] = signal.reasoning
            trace_data["is_cross_entity"] = signal.is_cross_entity
            trace_data["resolved_query"] = signal.resolved_query
            trace_data["params_merged"] = signal.entities.copy()
            attrs.update(trace_data)
            return signal

    # ── Bypass sentinel signal 构造 ──────────────────────────────────

    def _build_bypass_signal(
        self,
        *,
        query: str,
        intent_kind: IntentKind,
        params: dict[str, Any],
    ) -> QuerySignal:
        """为 CHITCHAT / META / RECALL 构造 sentinel 信号。

        三类下游处理由 orchestrator 完成：
        - CHITCHAT → 友好拒答模板
        - META → 系统能力清单模板
        - RECALL → ReAct 路径，让 Agent 看短期记忆
        """
        keyword_map = {
            IntentKind.CHITCHAT: _KW_CHITCHAT,
            IntentKind.META: _KW_META,
            IntentKind.RECALL: _KW_RECALL,
        }
        reasoning_map = {
            IntentKind.CHITCHAT: "前置 bypass：闲聊/问候/否定确认",
            IntentKind.META: "前置 bypass：系统能力/帮助询问",
            IntentKind.RECALL: "前置 bypass：明确回溯历史会话",
        }
        return QuerySignal(
            raw_query=query,
            intent_kind=intent_kind,
            keywords=[keyword_map[intent_kind]],
            entities=params,
            time_range_days=params.get("days"),
            route_level=0,
            confidence=_BYPASS_CONFIDENCE,
            reasoning=reasoning_map[intent_kind],
        )

    # ── 辅助方法 ─────────────────────────────────────────────────────

    @staticmethod
    def _resolve_type_from_signal(signal: QuerySignal) -> AnalysisType:
        """从 QuerySignal 的 keywords 中还原 AnalysisType。"""
        if not signal.keywords:
            return AnalysisType.COMPREHENSIVE
        try:
            return AnalysisType(signal.keywords[0])
        except ValueError:
            return AnalysisType.COMPREHENSIVE
