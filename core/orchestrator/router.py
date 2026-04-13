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

from api.schemas.analysis import AnalysisType
from config.settings import Settings, get_settings
from core.logging_utils import get_logger
from core.orchestrator.signal import QuerySignal

_logger = get_logger(__name__)

# 种子库 YAML 路径
_SEEDS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "intent_seeds.yaml"

# Chroma collection 名
_SEEDS_COLLECTION = "intent_seeds"

# ── 分析师角色描述（L3 LLM prompt 注入） ─────────────────────────────

_ROLE_DESCRIPTIONS: dict[str, str] = {
    "general": "",
    "procurement": (
        "采购分析师，重点关注采购订单、价格差异、供应商选择、"
        "采购支出和三路匹配等采购执行层面的问题。"
    ),
    "finance": (
        "财务合规人员，重点关注付款逾期、折扣利用率、发票重复、"
        "付款合规和资金风险等财务层面的问题。"
    ),
    "supply_chain": (
        "供应链经理，重点关注供应商绩效、交期、收货异常、"
        "供应商集中度和采购周期等供应链层面的问题。"
    ),
    "audit": (
        "审计人员，重点关注发票重复、付款合规、三路匹配异常、"
        "折扣滥用等合规审计层面的问题。"
    ),
    "management": (
        "管理层，重点关注采购支出趋势、供应商集中度、"
        "供应商绩效全局概览等战略层面的问题。"
    ),
}


# ── 非分析意图检测（方案 C：跳过 L1/L2，直达 L3）──────────────────────

# 回溯/对话引用模式——命中后还需检查是否含分析关键词
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

# 非回溯类 bypass：闲聊/能力询问/否定/确认（直接 bypass，无需二次校验）
_DIRECT_BYPASS_PATTERNS: list[re.Pattern[str]] = [
    # 闲聊/问候/感谢
    re.compile(r"^(你好|您好|hello|hi|hey|谢谢|感谢|thanks|thank you|再见|拜拜|bye)\s*[!！。.]*$", re.IGNORECASE),
    # 能力/帮助询问
    re.compile(
        r"你能做什么|你会什么|有什么功能|怎么用|帮助|help|"
        r"支持哪些|可以做什么|有哪些分析",
    ),
    # 否定/取消
    re.compile(r"^(不需要了|算了|取消|不用了|没事了|好的|知道了|明白了|ok|okay)\s*[。.!！]*$", re.IGNORECASE),
    # 纯确认/追问（无实质分析内容）
    re.compile(r"^(是的|对|嗯|好|可以|继续|然后呢|接下来呢|还有呢|详细说说)\s*[？?。.!！]*$"),
]

# 分析关键词——查询中含这些词说明用户有具体分析意图，不应被 bypass
_ANALYSIS_KEYWORDS: set[str] = {
    # 分析动词
    "分析", "检查", "查看", "查询", "评估", "计算", "统计", "对比", "比较", "审查",
    "analyze", "check", "review", "evaluate", "calculate",
    # 数据对象
    "订单", "采购", "供应商", "发票", "付款", "收货", "价格", "三路匹配",
    "po", "supplier", "invoice", "payment", "receipt",
    # 时间范围修饰
    "天", "月", "年", "季度", "周",
    "days", "months", "month", "year", "week",
    # 业务关键词
    "异常", "差异", "逾期", "合规", "绩效", "kpi", "支出", "折扣",
    "重复", "周期", "集中度", "风险",
}

# L2 置信度长度比打折阈值（方案 D）
_L2_LENGTH_RATIO_THRESHOLD = 0.4
_L2_LENGTH_RATIO_DISCOUNT = 0.7


def _has_analysis_keywords(query: str) -> bool:
    """检查 query 中是否包含分析关键词。"""
    q_lower = query.lower()
    return any(kw in q_lower for kw in _ANALYSIS_KEYWORDS)


def _is_non_analysis_query(query: str) -> bool:
    """检测 query 是否为非分析意图（会话回溯/闲聊/操作指令等）。

    判断逻辑：
    1. 直接 bypass 类（闲聊/能力询问/否定/确认）→ 直接返回 True
    2. 明确回溯类（上次/刚才/结果呢/再说一遍）→ 直接返回 True
       （"上次分析的结果呢"中的"分析"是回溯上下文，不是分析指令）
    3. 歧义回溯类（之前/前面/previous）→ 不含分析关键词时返回 True
       （"分析之前30天的价格差异"中"之前"是时间修饰，不是回溯）
    4. 其余 → 返回 False
    """
    q = query.strip()

    # 直接 bypass 类
    for pattern in _DIRECT_BYPASS_PATTERNS:
        if pattern.search(q):
            return True

    # 明确回溯类：语义明确是回溯，直接 bypass（不做分析关键词校验）
    for pattern in _RECALL_PATTERNS:
        if pattern.search(q):
            return True

    # 歧义回溯类：需要排除"时间范围修饰"用法
    for pattern in _AMBIGUOUS_RECALL_PATTERNS:
        if pattern.search(q):
            if not _has_analysis_keywords(q):
                return True
            return False  # 含分析关键词（如"分析之前30天的价格差异"），不 bypass

    return False


# ── Level 1 规则库 ────────────────────────────────────────────────────

_RULE_LIBRARY: list[dict[str, Any]] = [
    {
        "analysis_type": AnalysisType.THREE_WAY_MATCH,
        "keywords": {
            "三路匹配", "三单", "匹配", "three way", "three-way", "3way",
            "发票", "收货", "invoice", "goods receipt", "mismatch",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.PRICE_VARIANCE,
        "keywords": {
            "价格差异", "价格", "price", "variance", "ppv",
            "标准价", "合同价", "成本", "涨价", "单价",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.PAYMENT_COMPLIANCE,
        "keywords": {
            "付款", "逾期", "payment", "overdue", "到期",
            "应付", "账期", "折扣", "提前付款",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.SUPPLIER_PERFORMANCE,
        "keywords": {
            "供应商", "绩效", "kpi", "supplier", "performance",
            "准时交货", "交期", "质量", "评分", "scorecard", "otif",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.SPEND_ANALYSIS,
        "keywords": {
            "支出", "spend", "采购金额", "花费", "费用",
            "品类", "支出分布", "采购额", "开支",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.RECEIPT_ANOMALY,
        "keywords": {
            "收货", "超量", "拒收", "退货", "延迟收货",
            "receipt", "过量", "短缺", "入库异常",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.INVOICE_DUPLICATE,
        "keywords": {
            "重复发票", "duplicate", "重复", "相同发票",
            "发票", "重复开票", "重复付款",
        },
        "threshold": 0.20,
    },
    {
        "analysis_type": AnalysisType.DISCOUNT_UTILIZATION,
        "keywords": {
            "折扣", "discount", "早付", "提前付款折扣",
            "折扣利用", "现金折扣", "节省",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.PO_CYCLE_TIME,
        "keywords": {
            "周期", "cycle", "耗时", "时效",
            "从下单到", "处理时间", "lead time", "效率",
        },
        "threshold": 0.15,
    },
    {
        "analysis_type": AnalysisType.VENDOR_CONCENTRATION,
        "keywords": {
            "集中度", "依赖", "concentration", "单一来源",
            "供应商", "占比", "垄断", "多元化",
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
    "receipt_number": [r"RCV-\d[\da-zA-Z_-]*\d", r"RCV-\d+"],
    "days": [r"(?:最近|过去|近)\s*(\d+)\s*天", r"(?:past|last|recent)\s+(\d+)\s*days?"],
}


class IntentRouter:
    """三级意图路由器。

    替换原 IntentParser，提供 parse() 方法保持接口兼容。
    内部按 Level 1 → 2 → 3 逐级尝试，首次命中即返回。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings: Settings = settings or get_settings()
        self._seeds_store: Any = None  # 延迟初始化的 VectorStore
        self._seeds_loaded: bool = False
        self._llm: Any = None  # 延迟初始化的 ChatOpenAI

    # ── 公开接口 ───────────────────────────────────────────────────

    def route(self, query: str, analyst_role: str = "general") -> QuerySignal:
        """路由查询，返回完整的 QuerySignal（供 Orchestrator DAG 分支使用）。"""
        signal = self._route(query, analyst_role=analyst_role)
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

    def _route(self, query: str, analyst_role: str = "general") -> QuerySignal:
        """三级路由主逻辑，记录完整决策过程到 trace。"""
        from core.observability.middleware import record_span

        params = _extract_params(query, self._settings.analysis.entity_patterns)
        trace_data: dict[str, Any] = {
            "query": query,
            "params_regex": params.copy(),
        }

        with record_span("intent", "route_decision") as attrs:
            # 前置过滤：非分析意图的查询（回溯/闲聊/操作指令）跳过 L1/L2
            bypass = _is_non_analysis_query(query)
            trace_data["bypass_l1_l2"] = bypass

            if not bypass:
                # Level 1：关键词命中率
                l1_scores = self._evaluate_all_rules(query)
                trace_data["l1_scores"] = l1_scores

                signal = self._try_level1(query, params)
                if signal is not None:
                    trace_data["hit_level"] = 1
                    trace_data["result_type"] = signal.keywords[0] if signal.keywords else ""
                    trace_data["confidence"] = signal.confidence
                    trace_data["reasoning"] = signal.reasoning
                    attrs.update(trace_data)
                    return signal

                # Level 2：Chroma 语义匹配
                l2_results = self._search_seeds_for_trace(query)
                trace_data["l2_results"] = l2_results

                signal = self._try_level2(query, params)
                if signal is not None:
                    trace_data["hit_level"] = 2
                    trace_data["result_type"] = signal.keywords[0] if signal.keywords else ""
                    trace_data["confidence"] = signal.confidence
                    trace_data["reasoning"] = signal.reasoning
                    attrs.update(trace_data)
                    return signal

            # Level 3：LLM 分类（注入角色偏好）
            signal = self._try_level3(query, params, analyst_role=analyst_role)
            trace_data["hit_level"] = 3
            trace_data["result_type"] = signal.keywords[0] if signal.keywords else ""
            trace_data["confidence"] = signal.confidence
            trace_data["reasoning"] = signal.reasoning
            trace_data["params_merged"] = signal.entities.copy()
            attrs.update(trace_data)
            return signal

    # ── Trace 辅助方法 ────────────────────────────────────────────────

    def _evaluate_all_rules(self, query: str) -> list[dict[str, Any]]:
        """评估所有 L1 规则并返回评分详情（仅用于 trace，不影响路由）。"""
        query_lower = query.lower()
        scores = []
        for rule in _RULE_LIBRARY:
            rule_keywords: set[str] = rule["keywords"]
            hits = [kw for kw in rule_keywords if kw in query_lower]
            hit_rate = len(hits) / len(rule_keywords)
            scores.append({
                "rule": rule["analysis_type"].value,
                "hit_rate": round(hit_rate, 3),
                "threshold": rule["threshold"],
                "matched": hit_rate >= rule["threshold"],
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
            _logger.debug("_search_seeds_for_trace vector search failed: %s", exc)
            return []

    # ── Level 1：关键词命中率 ────────────────────────────────────────

    def _try_level1(
        self, query: str, params: dict[str, Any]
    ) -> QuerySignal | None:
        """关键词命中率评分匹配。

        对每条规则计算命中率 = 命中关键词数 / 规则关键词总数。
        使用子串匹配（kw in query_lower），适配中文无空格分词场景。
        """
        query_lower = query.lower()

        best_rule: dict[str, Any] | None = None
        best_score: float = 0.0

        for rule in _RULE_LIBRARY:
            rule_keywords: set[str] = rule["keywords"]
            hits = sum(1 for kw in rule_keywords if kw in query_lower)
            hit_rate = hits / len(rule_keywords)

            if hit_rate >= rule["threshold"] and hit_rate > best_score:
                best_score = hit_rate
                best_rule = rule

        if best_rule is None:
            return None

        analysis_type: AnalysisType = best_rule["analysis_type"]
        return QuerySignal(
            raw_query=query,
            keywords=[analysis_type.value],
            entities=params,
            time_range_days=params.get("days"),
            route_level=1,
            confidence=round(best_score, 3),
            reasoning=f"L1 关键词命中率 {best_score:.1%}，匹配 {analysis_type.value}",
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
            results = store.search(query=query, top_k=1)
        except Exception as exc:
            _logger.warning("intent seeds search failed, skip L2: %s", exc)
            return None

        if not results:
            return None

        best = results[0]
        distance: float = best.get("distance", 999.0)
        # Chroma cosine distance → similarity = 1 - distance
        similarity = 1.0 - distance

        # 方案 D：query 远短于种子文本时，相似度可能虚高，打折处理
        seed_text: str = best.get("text", "")
        length_ratio = len(query) / max(len(seed_text), 1)
        if length_ratio < _L2_LENGTH_RATIO_THRESHOLD:
            original = similarity
            similarity *= _L2_LENGTH_RATIO_DISCOUNT
            _logger.debug(
                "L2 length-ratio discount: query=%d seed=%d ratio=%.2f sim=%.3f→%.3f",
                len(query), len(seed_text), length_ratio, original, similarity,
            )

        if similarity < 0.80:
            return None

        matched_type_str: str = best.get("metadata", {}).get("analysis_type", "")
        try:
            analysis_type = AnalysisType(matched_type_str)
        except ValueError:
            return None

        return QuerySignal(
            raw_query=query,
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

    # ── Level 3：LLM 分类 ───────────────────────────────────────────

    _LLM_CLASSIFY_PROMPT = """你是 ERP 采购分析系统的意图分类器。根据用户查询，判断最匹配的分析类型。
{role_section}
可选分析类型：
- three_way_match: 采购订单、收货单、发票的三单匹配异常检查
- price_variance: 实际采购价格与合同价/标准价的偏差分析
- payment_compliance: 付款逾期、提前付款、折扣滥用等合规检查
- supplier_performance: 供应商交期、质量、KPI 综合绩效评估
- spend_analysis: 按品类/供应商维度的采购支出分布分析
- receipt_anomaly: 超量收货、拒收、延迟收货等收货异常分析
- invoice_duplicate: 重复发票检测
- discount_utilization: 早付折扣利用率分析
- po_cycle_time: 采购订单全流程周期分析
- vendor_concentration: 供应商集中度与采购依赖风险分析
- comprehensive: 以上多类或无法明确归类的综合分析

输出纯 JSON，无其他文字：
{{"type": "分析类型", "confidence": 0.0到1.0, "supplier_id": null或字符串, "po_number": null或字符串, "days": null或整数}}

用户查询：{query}"""

    def _ensure_llm(self) -> Any:
        """延迟初始化 LLM 客户端。"""
        if self._llm is not None:
            return self._llm

        from modules.p2p.model_factory import build_chat_model

        self._llm = build_chat_model(self._settings.llm)
        return self._llm

    def _try_level3(
        self, query: str, params: dict[str, Any], analyst_role: str = "general"
    ) -> QuerySignal:
        """LLM 分类兜底，始终返回结果。"""
        from core.observability.middleware import (
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
            prompt = self._LLM_CLASSIFY_PROMPT.format(
                query=query, role_section=role_section
            )

            # 显式记录 model span（L3 不经过 LangChain 中间件）
            model_name = getattr(llm, "model_name", None) or getattr(llm, "model", "unknown")
            with record_span("model", str(model_name)) as model_attrs:
                model_attrs["model"] = str(model_name)
                model_attrs["input"] = prompt[:2000]
                model_attrs["estimated_input_tokens"] = estimate_tokens(prompt)
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
            analysis_type_str: str = data.get("type", "comprehensive")
            try:
                analysis_type = AnalysisType(analysis_type_str)
            except ValueError:
                analysis_type = AnalysisType.COMPREHENSIVE

            # LLM 提取的参数合并到正则提取的参数上（正则优先）
            llm_params: dict[str, Any] = {}
            if data.get("supplier_id"):
                llm_params["supplier_id"] = data["supplier_id"]
            if data.get("po_number"):
                llm_params["po_number"] = data["po_number"]
            if data.get("days") is not None:
                llm_params["days"] = int(data["days"])

            merged_params = {**llm_params, **params}  # 正则提取的覆盖 LLM 的
            confidence: float = data.get("confidence", 0.5)

            return QuerySignal(
                raw_query=query,
                keywords=[analysis_type.value],
                entities=merged_params,
                time_range_days=merged_params.get("days"),
                route_level=3,
                confidence=round(confidence, 3),
                reasoning=f"L3 LLM 分类为 {analysis_type.value}，置信度 {confidence:.1%}",
            )

        except Exception as exc:
            _logger.warning("L3 LLM classify failed: %s, fallback to COMPREHENSIVE", exc)
            return QuerySignal(
                raw_query=query,
                keywords=[AnalysisType.COMPREHENSIVE.value],
                entities=params,
                time_range_days=params.get("days"),
                route_level=3,
                confidence=0.0,
                reasoning=f"L3 LLM 分类失败（{exc}），降级为 COMPREHENSIVE",
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
