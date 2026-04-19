"""
三级意图路由器。

Level 1：关键词命中率评分（零延迟）
Level 2：Chroma 种子库语义匹配（毫秒级）
Level 3：LLM 分类 + 参数提取（0.5~2s）

输出仍为 (AnalysisType, params)，与原 IntentParser 接口兼容，
Orchestrator 无需感知内部路由层级。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from api.schemas.domain import AnalysisType
from config.settings import Settings, get_settings
from core.logging_utils import get_logger
from core.orchestrator.signal import IntentKind, QuerySignal

# L2/L3 输出的 sentinel 字符串（写入 ``signal.keywords[0]``，与 AnalysisType
# 枚举共用同一字段，便于 trace 展示与 orchestrator 分支判断）。
_KW_DATA_LOOKUP = "data_lookup"
_KW_META = "meta"
_KW_CHITCHAT = "chitchat"
_KW_OUT_OF_SCOPE = "out_of_scope"
_KW_CLARIFICATION = "clarification"
_KW_RECALL = "recall"

# L1 / L2 / L3 默认置信度（用于 bypass 命中或 sentinel 信号）
_BYPASS_CONFIDENCE = 0.95
_LOOKUP_L1_CONFIDENCE = 0.7  # L1 命中 lookup 动词的固定置信度

_logger = get_logger(__name__)

# 种子库 YAML 路径
_SEEDS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "intent_seeds.yaml"

# Chroma collection 名
_SEEDS_COLLECTION = "intent_seeds"

# ── P2P 规则常量（从 modules/p2p/intent_rules.py 导入） ──────────────
from modules.p2p.intent_rules import (
    ANALYSIS_KEYWORDS as _ANALYSIS_KEYWORDS,
    LOOKUP_ENTITIES as _LOOKUP_ENTITIES,
    LOOKUP_MODIFIERS as _LOOKUP_MODIFIERS,
    LOOKUP_VERBS as _LOOKUP_VERBS,
    ROLE_DESCRIPTIONS as _ROLE_DESCRIPTIONS,
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


def _looks_like_data_lookup(query: str) -> bool:
    """启发式判断：query 是否为 DATA_LOOKUP（纯事实查询）。

    判定标准：同时包含 lookup 动词与业务实体；或者包含修饰词（最新/最近）
    与业务实体（"最新的 PO 是哪一个"）。

    注意：本函数仅在 query **未命中任何 analysis 规则**时被调用，所以
    不需要担心和分析意图冲突——L1 优先做 analysis 判定。
    """
    q_lower = query.lower()
    has_verb = any(v in q_lower for v in _LOOKUP_VERBS)
    has_entity = any(e in q_lower for e in _LOOKUP_ENTITIES)
    has_modifier = any(m in q_lower for m in _LOOKUP_MODIFIERS)
    return (has_verb and has_entity) or (has_modifier and has_entity)


# ── Level 1 规则库 ────────────────────────────────────────────────────

_RULE_LIBRARY: list[dict[str, Any]] = [
    {
        "analysis_type": AnalysisType.THREE_WAY_MATCH,
        "keywords": {
            "三路匹配", "三单", "匹配", "three way", "three-way", "3way",
            "发票", "收货", "invoice", "goods receipt", "mismatch",
            "三单核对", "三方对账", "单据不一致", "数量不符", "金额不符",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.PRICE_VARIANCE,
        "keywords": {
            "价格差异", "价格", "price", "variance", "ppv",
            "标准价", "合同价", "成本", "涨价", "单价",
            "价差分析", "单价波动", "溢价", "成本偏高", "比上次贵",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.PAYMENT_COMPLIANCE,
        "keywords": {
            "付款", "逾期", "payment", "overdue", "到期",
            "应付", "账期", "提前付款",
            "拖欠", "欠款", "延迟付款", "付款违规", "应付未付", "付款超期",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.SUPPLIER_PERFORMANCE,
        "keywords": {
            "供应商", "绩效", "kpi", "supplier", "performance",
            "准时交货", "交期", "质量", "评分", "scorecard", "otif",
            "交期表现", "供货质量", "交付率", "供应商等级",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.SPEND_ANALYSIS,
        "keywords": {
            "支出", "spend", "采购金额", "花费", "费用",
            "品类", "支出分布", "采购额", "开支",
            "采购总额", "品类支出", "支出占比", "花销分布",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.RECEIPT_ANOMALY,
        "keywords": {
            "超量", "拒收", "退货", "延迟收货",
            "receipt", "过量", "短缺", "入库异常",
            "多收", "少收", "收货异常", "验收不合格", "验货失败",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.INVOICE_DUPLICATE,
        "keywords": {
            "重复发票", "duplicate", "重复", "相同发票",
            "重复开票", "重复付款",
            "双开", "一票两付", "重复入账", "付两次", "开重了",
        },
        "threshold": 0.20,
    },
    {
        "analysis_type": AnalysisType.DISCOUNT_UTILIZATION,
        "keywords": {
            "折扣", "discount", "早付", "提前付款折扣",
            "折扣利用", "现金折扣", "节省",
            "折扣损失", "折扣利用率", "错过折扣", "应得折扣",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.PO_CYCLE_TIME,
        "keywords": {
            "周期", "cycle", "耗时", "时效",
            "从下单到", "处理时间", "lead time", "效率",
            "处理慢", "审批太久", "多久能到", "处理快慢",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.VENDOR_CONCENTRATION,
        "keywords": {
            "集中度", "依赖", "concentration", "单一来源",
            "占比", "垄断", "多元化",
            "太集中", "依赖度", "供应商太少", "单一供应商", "多元化不足",
        },
        "threshold": 0.20,
    },
]


# ── 参数提取（复用原 IntentParser 逻辑） ─────────────────────────────

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
    """三级意图路由器。

    替换原 IntentParser，提供 parse() 方法保持接口兼容。
    内部按 Level 1 → 2 → 3 逐级尝试，首次命中即返回。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings: Settings = settings or get_settings()
        self._seeds_store: Any = None  # 延迟初始化的 VectorStore（L2，保留供兼容）
        self._seeds_loaded: bool = False
        self._llm: Any = None  # 延迟初始化的 ChatOpenAI（L3，保留供兼容）
        self._case_store: Any = None  # 延迟注入的 DAGCaseStore
        self._unified_router: Any = None  # 统一 LLM 路由器（延迟初始化）
        _ir = self._settings.intent_routing
        _logger.info(
            "intent_routing settings: l1=%.2f/%.2f l2_sim=%.2f l2_topk=%d "
            "l3_dag_min=%.2f l25=%s generic_tpl=%s",
            _ir.l1_threshold_default, _ir.l1_threshold_strict,
            _ir.l2_similarity_threshold, _ir.l2_topk,
            _ir.l3_dag_min_confidence, _ir.l25_enabled,
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

    # ── Trace 辅助方法 ────────────────────────────────────────────────

    def _evaluate_all_rules(self, query: str) -> list[dict[str, Any]]:
        """评估所有 L1 规则并返回评分详情（仅用于 trace，不影响路由）。"""
        query_lower = query.lower()
        cfg = self._settings.intent_routing
        scores = []
        for rule in _RULE_LIBRARY:
            rule_keywords: set[str] = rule["keywords"]
            hits = [kw for kw in rule_keywords if kw in query_lower]
            hit_rate = len(hits) / len(rule_keywords)
            effective_threshold = (
                cfg.l1_threshold_strict
                if rule["threshold"] >= 0.20
                else cfg.l1_threshold_default
            )
            scores.append({
                "rule": rule["analysis_type"].value,
                "hit_rate": round(hit_rate, 3),
                "threshold": effective_threshold,
                "matched": hit_rate >= effective_threshold,
                "hit_keywords": hits,
            })
        return scores

    def _search_seeds_for_trace(self, query: str) -> list[dict[str, Any]]:
        """执行 L2 种子库检索并返回 top-3 结果（仅用于 trace，不影响路由）。"""
        try:
            store = self._ensure_seeds_store()
            results = store.search(query=query, top_k=3)
            return [
                {
                    "seed_text": r.get("text", "")[:80],
                    "analysis_type": r.get("metadata", {}).get("analysis_type", ""),
                    "similarity": round(1.0 - r.get("distance", 999.0), 3),
                }
                for r in results
            ]
        except Exception as exc:
            _logger.info("_search_seeds_for_trace vector search failed: %s", exc)
            return []

    # ── Level 1：关键词命中率 ────────────────────────────────────────

    def _try_level1(
        self, query: str, params: dict[str, Any]
    ) -> QuerySignal | None:
        """关键词命中率评分匹配。

        对每条规则计算命中率 = 命中关键词数 / 规则关键词总数。
        使用子串匹配（kw in query_lower），适配中文无空格分词场景。

        若 analysis 规则全部 miss，则尝试判定 DATA_LOOKUP（lookup 动词 +
        业务实体）——这是 "查询最新的一个 po" 这类纯事实查询的兜底入口。
        """
        query_lower = query.lower()

        best_rule: dict[str, Any] | None = None
        best_score: float = 0.0

        for rule in _RULE_LIBRARY:
            rule_keywords: set[str] = rule["keywords"]
            hits = sum(1 for kw in rule_keywords if kw in query_lower)
            hit_rate = hits / len(rule_keywords)

            cfg = self._settings.intent_routing
            threshold = (
                cfg.l1_threshold_strict
                if rule["threshold"] >= 0.20
                else cfg.l1_threshold_default
            )
            if hit_rate >= threshold and hit_rate > best_score:
                best_score = hit_rate
                best_rule = rule

        if best_rule is not None:
            analysis_type: AnalysisType = best_rule["analysis_type"]
            return QuerySignal(
                raw_query=query,
                intent_kind=IntentKind.ANALYSIS,
                keywords=[analysis_type.value],
                entities=params,
                time_range_days=params.get("days"),
                route_level=1,
                confidence=round(best_score, 3),
                reasoning=f"L1 关键词命中率 {best_score:.1%}，匹配 {analysis_type.value}",
            )

        # analysis 规则未命中——尝试 DATA_LOOKUP 兜底
        if _looks_like_data_lookup(query):
            return QuerySignal(
                raw_query=query,
                intent_kind=IntentKind.DATA_LOOKUP,
                keywords=[_KW_DATA_LOOKUP],
                entities=params,
                time_range_days=params.get("days"),
                route_level=1,
                confidence=_LOOKUP_L1_CONFIDENCE,
                reasoning="L1 lookup 动词+实体命中，判定为事实查询",
            )

        return None

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

    # ── Level 2：Chroma 种子库语义匹配 ──────────────────────────────

    def _ensure_seeds_store(self) -> Any:
        """延迟初始化 Chroma intent_seeds collection 并加载种子数据。"""
        if self._seeds_store is not None:
            return self._seeds_store

        from core.knowledge.vector_store import VectorStore

        store = VectorStore.from_settings(self._settings, _SEEDS_COLLECTION)
        store.initialize()

        # 仅首次加载种子数据
        if not self._seeds_loaded:
            self._load_seeds(store)
            self._seeds_loaded = True

        self._seeds_store = store
        return store

    def _load_seeds(self, store: Any) -> None:
        """从 YAML 加载种子问题并写入 Chroma（幂等 upsert）。"""
        if not _SEEDS_PATH.exists():
            _logger.warning("intent seeds file not found: %s", _SEEDS_PATH)
            return

        with open(_SEEDS_PATH, encoding="utf-8") as f:
            seeds_data: dict[str, list[str]] = yaml.safe_load(f) or {}

        docs: list[dict[str, Any]] = []
        for analysis_type_str, questions in seeds_data.items():
            for idx, question in enumerate(questions):
                docs.append({
                    "id": f"seed_{analysis_type_str}_{idx}",
                    "text": question,
                    "metadata": {"analysis_type": analysis_type_str},
                })

        if docs:
            store.add_documents(docs)
            _logger.info("loaded %d intent seeds into Chroma", len(docs))

    @staticmethod
    def _aggregate_l2_votes(
        results: list[dict[str, Any]],
        similarity_threshold: float,
    ) -> tuple[str | None, float, str]:
        """Top-k 多数投票：按 analysis_type 分桶，取票数最多的桶。

        投票规则（简化版，不用权重）：
        1. 计算每条结果的 similarity = 1 - distance
        2. 按 analysis_type 分桶，统计每桶的票数和最高 similarity
        3. 取票数最多的桶；平票时取最高 similarity 的桶
        4. 该桶的最高 similarity >= threshold → 命中
        5. 否则返回 None

        Returns:
            (analysis_type_str | None, best_similarity, reasoning)
        """
        from collections import Counter

        if not results:
            return None, 0.0, "L2 voting: empty results"

        votes: dict[str, list[float]] = {}
        for r in results:
            atype = r.get("metadata", {}).get("analysis_type", "")
            if not atype:
                continue
            sim = 1.0 - r.get("distance", 999.0)
            votes.setdefault(atype, []).append(sim)

        if not votes:
            return None, 0.0, "L2 voting: no valid types"

        # Sort by: (vote_count DESC, max_similarity DESC)
        ranked = sorted(
            votes.items(),
            key=lambda kv: (len(kv[1]), max(kv[1])),
            reverse=True,
        )
        winner_type, winner_sims = ranked[0]
        best_sim = max(winner_sims)
        vote_count = len(winner_sims)
        total = sum(len(v) for v in votes.values())

        reasoning = (
            f"L2 top-k 投票：{winner_type} 得票 {vote_count}/{total}，"
            f"最高相似度 {best_sim:.1%}"
        )

        if best_sim >= similarity_threshold:
            return winner_type, best_sim, reasoning

        return None, best_sim, reasoning + "（未达阈值）"

    def _try_level2(
        self, query: str, params: dict[str, Any]
    ) -> QuerySignal | None:
        """Chroma 种子库语义相似度匹配。"""
        try:
            store = self._ensure_seeds_store()
        except Exception as exc:
            _logger.warning("intent seeds store init failed, skip L2: %s", exc)
            return None

        try:
            results = store.search(query=query, top_k=self._settings.intent_routing.l2_topk)
        except Exception as exc:
            _logger.warning("intent seeds search failed, skip L2: %s", exc)
            return None

        if not results:
            return None

        cfg = self._settings.intent_routing
        # Top-k voting when topk > 1
        if cfg.l2_topk > 1 and len(results) > 1:
            winner_type, best_sim, reasoning = self._aggregate_l2_votes(
                results, cfg.l2_similarity_threshold,
            )
            if winner_type is None:
                return None

            # Sentinel types
            if winner_type in (_KW_DATA_LOOKUP, _KW_META):
                sentinel_kind = (
                    IntentKind.DATA_LOOKUP if winner_type == _KW_DATA_LOOKUP
                    else IntentKind.META
                )
                return QuerySignal(
                    raw_query=query,
                    intent_kind=sentinel_kind,
                    keywords=[winner_type],
                    entities=params,
                    time_range_days=params.get("days"),
                    route_level=2,
                    confidence=round(best_sim, 3),
                    reasoning=reasoning,
                )

            try:
                analysis_type = AnalysisType(winner_type)
            except ValueError:
                return None

            return QuerySignal(
                raw_query=query,
                intent_kind=IntentKind.ANALYSIS,
                keywords=[analysis_type.value],
                entities=params,
                time_range_days=params.get("days"),
                route_level=2,
                confidence=round(best_sim, 3),
                reasoning=reasoning,
            )

        # Original top-1 logic (topk=1 or only 1 result)
        best = results[0]
        distance: float = best.get("distance", 999.0)
        # Chroma cosine distance → similarity = 1 - distance
        similarity = 1.0 - distance

        # 方案 D：query 远短于种子文本时，相似度可能虚高，打折处理
        seed_text: str = best.get("text", "")
        length_ratio = len(query) / max(len(seed_text), 1)
        _ir = self._settings.intent_routing
        if length_ratio < _ir.l2_length_ratio_floor:
            original = similarity
            similarity *= _ir.l2_length_ratio_penalty
            _logger.info(
                "L2 length-ratio discount: query=%d seed=%d ratio=%.2f sim=%.3f→%.3f",
                len(query), len(seed_text), length_ratio, original, similarity,
            )

        if similarity < self._settings.intent_routing.l2_similarity_threshold:
            return None

        matched_type_str: str = best.get("metadata", {}).get("analysis_type", "")

        # 种子库支持 sentinel 类目：data_lookup / meta（非 AnalysisType 枚举值）
        if matched_type_str == _KW_DATA_LOOKUP:
            _logger.info(
                "L2 matched data_lookup: similarity=%.3f seed='%s'",
                similarity, (best.get("text", "") or "")[:30],
            )
            return QuerySignal(
                raw_query=query,
                intent_kind=IntentKind.DATA_LOOKUP,
                keywords=[_KW_DATA_LOOKUP],
                entities=params,
                time_range_days=params.get("days"),
                route_level=2,
                confidence=round(similarity, 3),
                reasoning=(
                    f"L2 语义匹配（事实查询）'{best.get('text', '')[:30]}…'，"
                    f"相似度 {similarity:.1%}"
                ),
            )

        if matched_type_str == _KW_META:
            _logger.info(
                "L2 matched meta: similarity=%.3f seed='%s'",
                similarity, (best.get("text", "") or "")[:30],
            )
            return QuerySignal(
                raw_query=query,
                intent_kind=IntentKind.META,
                keywords=[_KW_META],
                entities=params,
                route_level=2,
                confidence=round(similarity, 3),
                reasoning=(
                    f"L2 语义匹配（系统能力）'{best.get('text', '')[:30]}…'，"
                    f"相似度 {similarity:.1%}"
                ),
            )

        try:
            analysis_type = AnalysisType(matched_type_str)
        except ValueError:
            return None

        _logger.info(
            "L2 matched: type=%s similarity=%.3f seed='%s'",
            analysis_type.value, similarity, (best.get("text", "") or "")[:30],
        )
        return QuerySignal(
            raw_query=query,
            intent_kind=IntentKind.ANALYSIS,
            keywords=[analysis_type.value],
            entities=params,
            time_range_days=params.get("days"),
            route_level=2,
            confidence=round(similarity, 3),
            reasoning=(
                f"L2 语义匹配 '{best.get('text', '')[:30]}…'，"
                f"相似度 {similarity:.1%}"
            ),
        )

    # ── Level 2.5：案例库检索 ────────────────────────────────────────

    def _try_level25(
        self, query: str, params: dict[str, Any]
    ) -> QuerySignal | None:
        """L2.5 案例库检索：从历史成功 DAG 案例中匹配相似 query。

        仅在 ``intent_routing.l25_enabled=True`` 时生效。命中时返回
        route_level=25 的 signal，并通过 ``dag_hint`` 传递复用 DAG 定义。
        """
        if not self._settings.intent_routing.l25_enabled:
            return None

        if self._case_store is None:
            return None

        try:
            case = self._case_store.search_similar_case(query)
        except Exception as exc:
            _logger.warning("L2.5 case search failed: %s", exc)
            return None

        if case is None:
            return None

        analysis_type_str = case.get("analysis_type", "")
        dag_def = case.get("dag_definition")
        similarity = case.get("similarity", 0.0)

        _logger.info(
            "L2.5 case hit: type=%s similarity=%.3f tasks=%d",
            analysis_type_str, similarity, case.get("task_count", 0),
        )

        # 判断是否为合法 AnalysisType
        try:
            AnalysisType(analysis_type_str)
        except ValueError:
            _logger.warning("L2.5 case has invalid analysis_type: %s", analysis_type_str)
            return None

        return QuerySignal(
            raw_query=query,
            intent_kind=IntentKind.ANALYSIS,
            keywords=[analysis_type_str],
            entities=params,
            time_range_days=params.get("days"),
            route_level=25,
            confidence=similarity,
            dag_hint=dag_def if isinstance(dag_def, list) else None,
            reasoning=f"L2.5 案例命中，相似度 {similarity:.1%}",
        )

    # ── Level 3：LLM 分类 ───────────────────────────────────────────

    _LLM_CLASSIFY_PROMPT = """你是 ERP 采购分析系统的意图分类器。根据用户查询，先判定 intent_kind（意图大类），再在需要时给出具体 analysis_type（分析子类型）。

当前日期：{current_date}（{timezone}）。"最近 N 天"/"本月"/"上周"等相对时间以此为基准计算 days 参数。
{role_section}
## intent_kind 枚举（先选这个，再决定其他字段）
- analysis: 用户要做某种异常检测/合规检查/绩效评估等"分析"工作（必须能落入下面 11 类 analysis_type 之一）。**即使用户未指定时间范围、供应商、PO 编号等参数，只要分析意图明确就判 analysis**，系统会自动使用默认参数执行。
- data_lookup: 纯事实查询/单据检索（"查最新 PO"/"列出 SUP-001 的发票"/"看看这单的金额"），不涉及异常或评估
- meta: 系统能力或数据元信息询问（"你支持哪些分析"/"数据更新到什么时候"）
- chitchat: 闲聊/问候/与采购无关的常识/情感问答（"今天天气"/"hi"）
- out_of_scope: 明确指向非 P2P 业务模块的场景（如"销售订单分析"/"HR 数据"/"生产排程"）。**若查询与采购流程有任何关联（即使间接），应优先判 analysis 并选最相关的 analysis_type**

## analysis_type 枚举（仅当 intent_kind=analysis 时填，否则置空字符串）
{analysis_types_section}

## 判定规则
1. 先判 intent_kind，再决定其他字段。**核心原则：尽量判为 analysis 让系统执行，而不是拒绝用户**。
2. 用户在"查/查询/查看/列出/最新/最近"等动词配合业务实体（PO/发票/供应商/订单等）时，优先判 data_lookup，而不是强行归入某个 analysis 类型。
3. 只有"明确无业务关联"的才判 chitchat（如问候/天气/闲聊）；只有"明确指向非 P2P 模块"才判 out_of_scope（如销售/HR/生产）。
4. 缺少时间范围、供应商、PO 编号等参数不影响判定，系统有默认参数可执行。意图模糊时归 analysis + comprehensive，**禁止返回 clarification**。
5. analysis 类型选最具体的；只有明确需要跨多个维度时才选 comprehensive，"模糊但相关"不要强行归为 comprehensive。

## confidence 判定锚点（严格按区间给分，不要一律给 0.8/0.9）
- >0.9: 查询直接命中某 intent_kind+type 的核心名词（如"三路匹配"/"重复发票"），语义无歧义
- 0.7-0.9: 语义强相关需要推断（如"发票和收货对不上"→analysis/three_way_match）
- 0.5-0.7: 多类共存或表述模糊
- <0.5: 表述极度模糊且无法推断出任何 analysis_type；归入 analysis + comprehensive 让系统尝试执行

## 参数抽取规则
- 只抽取用户查询中明确出现的具体值；代词或模糊引用（"上次那家"/"昨天的"）一律填 null。

## 输出格式（输出**纯 JSON**，不要 markdown 代码块、不要前后说明，第一个字符必须是 `{{`）
{{"intent_kind": "<枚举值>", "type": "<analysis_type 枚举值或空串>", "confidence": 0.0到1.0, "missing_params": [...], "supplier_id": null或字符串, "po_number": null或字符串, "days": null或整数}}

## 边界 case 示例
- "查询最新的一个 PO" → {{"intent_kind":"data_lookup","type":"","confidence":0.9,"missing_params":[],"supplier_id":null,"po_number":null,"days":null}}
- "做一下三路匹配" → {{"intent_kind":"analysis","type":"three_way_match","confidence":0.85,"missing_params":[],"supplier_id":null,"po_number":null,"days":null}}
- "分析一下付款合规" → {{"intent_kind":"analysis","type":"payment_compliance","confidence":0.85,"missing_params":[],"supplier_id":null,"po_number":null,"days":null}}
- "你支持哪些采购分析" → {{"intent_kind":"meta","type":"","confidence":0.95,"missing_params":[],"supplier_id":null,"po_number":null,"days":null}}
- "今天天气怎么样" → {{"intent_kind":"chitchat","type":"","confidence":0.95,"missing_params":[],"supplier_id":null,"po_number":null,"days":null}}
- "帮我看看销售订单的回款情况" → {{"intent_kind":"out_of_scope","type":"","confidence":0.9,"missing_params":[],"supplier_id":null,"po_number":null,"days":null}}
- "分析最近 30 天供应商 SUP-001 的价格差异" → {{"intent_kind":"analysis","type":"price_variance","confidence":0.95,"missing_params":[],"supplier_id":"SUP-001","po_number":null,"days":30}}
- "帮我看看采购" → {{"intent_kind":"analysis","type":"comprehensive","confidence":0.5,"missing_params":[],"supplier_id":null,"po_number":null,"days":null}}

用户查询：{query}"""

    def _ensure_llm(self) -> Any:
        """延迟初始化 LLM 客户端。

        L3 是单轮 11 路 JSON 分类，不需要链式推理；且输出要被 ``json.loads``
        解析，部分 provider 的思考内容会以 ``<think>`` 等形式混入 ``content``，
        拉低解析成功率。统一关闭 thinking。
        """
        if self._llm is not None:
            return self._llm

        from core.llm.model_factory import build_chat_model

        self._llm = build_chat_model(self._settings.llm_fast, disable_thinking=True)
        return self._llm

    def _try_level3(
        self, query: str, params: dict[str, Any], analyst_role: str = "general"
    ) -> QuerySignal:
        """LLM 分类兜底，始终返回结果。"""
        from core.observability.tracing import (
            _safe_jsonable,
            estimate_tokens,
            record_span,
        )

        try:
            llm = self._ensure_llm()
            role_desc = _ROLE_DESCRIPTIONS.get(analyst_role, "")
            role_section = (
                f"\n用户角色：{role_desc}\n请结合用户角色偏好判断最可能的分析意图。\n"
                if role_desc
                else ""
            )
            from core.time_utils import get_timezone_name, now_cn
            from modules.p2p.intent_rules import ANALYSIS_TYPE_DESCRIPTIONS

            types_section = "\n".join(
                f"- {name}: {desc}" for name, desc in ANALYSIS_TYPE_DESCRIPTIONS
            )

            prompt = self._LLM_CLASSIFY_PROMPT.format(
                query=query,
                role_section=role_section,
                analysis_types_section=types_section,
                current_date=now_cn().strftime("%Y-%m-%d"),
                timezone=get_timezone_name(),
            )

            import hashlib

            prompt_hash = hashlib.md5(prompt.encode("utf-8")).hexdigest()[:12]

            # 显式记录 model span（L3 不经过 LangChain 中间件）
            model_name = getattr(llm, "model_name", None) or getattr(llm, "model", "unknown")
            with record_span("model", str(model_name)) as model_attrs:
                model_attrs["model"] = str(model_name)
                model_attrs["input"] = prompt[:2000]
                model_attrs["estimated_input_tokens"] = estimate_tokens(prompt)
                model_attrs["prompt_hash"] = prompt_hash
                response = llm.invoke(prompt)
                content: str = response.content if hasattr(response, "content") else str(response)
                usage = (
                    getattr(response, "usage_metadata", None)
                    or getattr(response, "response_metadata", None)
                )
                model_attrs["output"] = {
                    "content": content[:2000],
                    "tool_calls": None,
                    "usage": _safe_jsonable(usage) if usage else None,
                }

            # 清理可能的 markdown 代码块
            raw = content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip().rstrip("```").strip()

            data = json.loads(raw)

            # LLM 提取的参数合并到正则提取的参数上（正则优先）
            llm_params: dict[str, Any] = {}
            if data.get("supplier_id"):
                llm_params["supplier_id"] = data["supplier_id"]
            if data.get("po_number"):
                llm_params["po_number"] = data["po_number"]
            if data.get("days") is not None:
                try:
                    llm_params["days"] = int(data["days"])
                except (TypeError, ValueError):
                    pass

            merged_params = {**llm_params, **params}
            confidence: float = float(data.get("confidence", 0.5))

            # intent_kind 字段（新版 prompt）；旧版无此字段时根据 type 兼容推断
            intent_kind_str: str = (data.get("intent_kind") or "").strip()
            analysis_type_str: str = (data.get("type") or "").strip()

            # 兼容老 prompt 输出（仅有 type，没有 intent_kind）
            if not intent_kind_str:
                intent_kind_str = IntentKind.ANALYSIS.value

            try:
                intent_kind = IntentKind(intent_kind_str)
            except ValueError:
                _logger.warning(
                    "L3 returned unknown intent_kind=%r, fallback to ANALYSIS",
                    intent_kind_str,
                )
                intent_kind = IntentKind.ANALYSIS

            # 各 intent_kind 的 keywords 与 reasoning 构造
            return self._build_l3_signal(
                query=query,
                intent_kind=intent_kind,
                analysis_type_str=analysis_type_str,
                confidence=confidence,
                missing_params=data.get("missing_params") or [],
                merged_params=merged_params,
            )

        except Exception as exc:
            _logger.warning("L3 LLM classify failed: %s, fallback to COMPREHENSIVE", exc)
            return QuerySignal(
                raw_query=query,
                intent_kind=IntentKind.ANALYSIS,
                keywords=[AnalysisType.COMPREHENSIVE.value],
                entities=params,
                time_range_days=params.get("days"),
                route_level=3,
                confidence=0.0,
                reasoning=f"L3 LLM 分类失败（{exc}），降级为 COMPREHENSIVE",
            )

    def _build_l3_signal(
        self,
        *,
        query: str,
        intent_kind: IntentKind,
        analysis_type_str: str,
        confidence: float,
        missing_params: list[str],
        merged_params: dict[str, Any],
    ) -> QuerySignal:
        """根据 L3 输出的 intent_kind 构造对应 QuerySignal。

        各 intent_kind 的 keywords 字段与 sentinel 常量对齐，方便 orchestrator
        与 trace 系统统一识别。
        """
        common = dict(
            raw_query=query,
            entities=merged_params,
            time_range_days=merged_params.get("days"),
            route_level=3,
            confidence=round(max(0.0, min(1.0, confidence)), 3),
        )
        # 清洗 missing_params 列表（防御 LLM 输出非字符串）
        cleaned_missing = [
            str(p).strip() for p in missing_params if isinstance(p, (str, int))
        ]

        if intent_kind == IntentKind.ANALYSIS:
            try:
                analysis_type = AnalysisType(analysis_type_str)
            except ValueError:
                analysis_type = AnalysisType.COMPREHENSIVE
            _logger.info(
                "L3 classified: kind=analysis type=%s confidence=%.3f entities=%s",
                analysis_type.value, confidence,
                {k: v for k, v in merged_params.items() if v},
            )
            return QuerySignal(
                **common,
                intent_kind=IntentKind.ANALYSIS,
                keywords=[analysis_type.value],
                reasoning=f"L3 LLM 分类为 {analysis_type.value}，置信度 {confidence:.1%}",
            )

        if intent_kind == IntentKind.DATA_LOOKUP:
            _logger.info(
                "L3 classified: kind=data_lookup confidence=%.3f entities=%s",
                confidence, {k: v for k, v in merged_params.items() if v},
            )
            return QuerySignal(
                **common,
                intent_kind=IntentKind.DATA_LOOKUP,
                keywords=[_KW_DATA_LOOKUP],
                reasoning=f"L3 判定为事实查询，置信度 {confidence:.1%}",
            )

        if intent_kind == IntentKind.CLARIFICATION:
            # CLARIFICATION 已废弃——降级为 ANALYSIS/comprehensive，
            # 遵循"尽量回复"原则，让 ReAct 尝试执行。
            kw = AnalysisType.COMPREHENSIVE.value
            try:
                AnalysisType(analysis_type_str)
                kw = analysis_type_str
            except ValueError:
                pass
            _logger.info(
                "L3 returned clarification → downgrade to analysis/%s "
                "confidence=%.3f",
                kw, confidence,
            )
            return QuerySignal(
                **common,
                intent_kind=IntentKind.ANALYSIS,
                keywords=[kw],
                reasoning=(
                    f"L3 原判 clarification，降级为 analysis/{kw}，"
                    f"置信度 {confidence:.1%}"
                ),
            )

        if intent_kind == IntentKind.META:
            return QuerySignal(
                **common,
                intent_kind=IntentKind.META,
                keywords=[_KW_META],
                reasoning="L3 判定为系统能力/帮助询问",
            )

        if intent_kind == IntentKind.CHITCHAT:
            return QuerySignal(
                **common,
                intent_kind=IntentKind.CHITCHAT,
                keywords=[_KW_CHITCHAT],
                reasoning="L3 判定为闲聊/非业务查询",
            )

        if intent_kind == IntentKind.OUT_OF_SCOPE:
            return QuerySignal(
                **common,
                intent_kind=IntentKind.OUT_OF_SCOPE,
                keywords=[_KW_OUT_OF_SCOPE],
                reasoning="L3 判定为业务相关但本系统不覆盖",
            )

        # RECALL 不应由 L3 输出（前置 bypass 已处理），保险起见按 ANALYSIS 兜底
        _logger.warning(
            "L3 returned RECALL via LLM (unexpected), treating as COMPREHENSIVE"
        )
        return QuerySignal(
            **common,
            intent_kind=IntentKind.ANALYSIS,
            keywords=[AnalysisType.COMPREHENSIVE.value],
            reasoning="L3 RECALL 兜底为 COMPREHENSIVE",
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
