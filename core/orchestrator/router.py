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
]


# ── 参数提取（复用原 IntentParser 逻辑） ─────────────────────────────

def _extract_params(query: str) -> dict[str, Any]:
    """从查询文本中用正则提取业务参数。"""
    params: dict[str, Any] = {}

    supplier_match = re.search(r"(?:SUP|sup|S)-?\d+[-\w]*", query)
    if supplier_match:
        params["supplier_id"] = supplier_match.group()

    po_match = re.search(r"(?:PO|po)-?[\d][\d\w-]*", query)
    if po_match:
        params["po_number"] = po_match.group()

    days_match = re.search(r"(?:最近|过去|近)\s*(\d+)\s*天", query)
    if not days_match:
        days_match = re.search(
            r"(?:past|last|recent)\s+(\d+)\s*days?", query, re.IGNORECASE
        )
    if days_match:
        params["days"] = int(days_match.group(1))

    return params


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

    def route(self, query: str) -> QuerySignal:
        """路由查询，返回完整的 QuerySignal（供 Orchestrator DAG 分支使用）。"""
        signal = self._route(query)
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

    def _route(self, query: str) -> QuerySignal:
        """三级路由主逻辑。"""
        params = _extract_params(query)

        # Level 1：关键词命中率
        signal = self._try_level1(query, params)
        if signal is not None:
            return signal

        # Level 2：Chroma 语义匹配
        signal = self._try_level2(query, params)
        if signal is not None:
            return signal

        # Level 3：LLM 分类
        return self._try_level3(query, params)

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

可选分析类型：
- three_way_match: 采购订单、收货单、发票的三单匹配异常检查
- price_variance: 实际采购价格与合同价/标准价的偏差分析
- payment_compliance: 付款逾期、提前付款、折扣滥用等合规检查
- supplier_performance: 供应商交期、质量、KPI 综合绩效评估
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

    def _try_level3(self, query: str, params: dict[str, Any]) -> QuerySignal:
        """LLM 分类兜底，始终返回结果。"""
        try:
            llm = self._ensure_llm()
            prompt = self._LLM_CLASSIFY_PROMPT.format(query=query)
            response = llm.invoke(prompt)
            content: str = response.content if hasattr(response, "content") else str(response)

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
