"""MemoryExtractor — 从分析结果中提取结构化记忆。

纯函数式提取，不持有状态。每种类型有独立的提取方法，互不影响。
单条提取失败不影响其他类型。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from config.settings import Settings
from core.memory.types import MemoryType
from core.memory.long_term import _inc
from core.time_utils import now_cn

_logger = logging.getLogger(__name__)


class MemoryExtractor:
    """从 AnalysisResult 中提取结构化记忆，写入长期记忆。"""

    def __init__(self, repo: Any, settings: Settings) -> None:
        self._repo = repo
        self._settings = settings

    async def extract(
        self,
        result: Any,
        user_id: str,
        session_id: str,
    ) -> dict[str, str | None]:
        """从分析结果提取所有类型的记忆，返回各类型写入的 memory_id。

        每种类型独立 try/except，单类型失败不影响其他。
        """
        from core.observability.tracing import record_span

        outcomes: dict[str, str | None] = {}

        with record_span("memory", "memory.extract", user_id=user_id) as span:
            # 1. entity_profile
            try:
                outcomes["entity_profile"] = self._extract_entity_profile(
                    result, user_id, session_id,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("extract entity_profile failed: %s", exc)
                outcomes["entity_profile"] = None

            # 2. analysis_insight
            try:
                outcomes["analysis_insight"] = self._extract_analysis_insight(
                    result, user_id, session_id,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("extract analysis_insight failed: %s", exc)
                outcomes["analysis_insight"] = None

            written = [k for k, v in outcomes.items() if v is not None]
            span["memory_types_written"] = written
            span["status"] = "ok"

            for t in written:
                _inc(f"ltm.extract.written.{t}")
            skipped = [k for k, v in outcomes.items() if v is None]
            for t in skipped:
                _inc(f"ltm.extract.skipped.{t}")

        return outcomes

    # ── entity_profile 提取 ──────────────────────────────────

    def _collect_entities(self, result: Any) -> list[tuple[str, str]]:
        """从分析结果中收集涉及的实体 (entity_type, entity_id) 对。

        来源：anomalies 中的 DocumentRef + supplier_kpis + summary。
        """
        entities: dict[tuple[str, str], None] = {}  # 保序去重

        # 从 anomalies 的 DocumentRef 中提取
        for anomaly in getattr(result, "anomalies", []):
            docs = getattr(anomaly, "documents", None)
            if docs is None:
                continue
            po = getattr(docs, "po_number", "")
            if po:
                entities[("po", po)] = None
            supplier = getattr(docs, "vendor_name", "")
            if supplier:
                entities[("supplier", supplier)] = None
            inv = getattr(docs, "invoice_num", "")
            if inv:
                entities[("invoice", inv)] = None

        # 从 supplier_kpis 中提取
        for kpi in getattr(result, "supplier_kpis", []):
            sid = getattr(kpi, "vendor_id", "")
            if sid:
                entities[("supplier", sid)] = None

        # 从 summary 中提取
        summary = getattr(result, "summary", {}) or {}
        for key in ("vendor_id", "po_number"):
            val = summary.get(key)
            if val:
                etype = "supplier" if "vendor" in key else "po"
                entities[(etype, val)] = None

        return list(entities.keys())

    def _extract_entity_profile(
        self, result: Any, user_id: str, session_id: str,
    ) -> str | None:
        """从分析结果中提取实体画像。

        为每个涉及的实体生成一条 entity_profile 记忆。
        """
        entities = self._collect_entities(result)
        if not entities:
            return None

        analysis_type = getattr(result, "analysis_type", None)
        analysis_type_val = analysis_type.value if analysis_type else "unknown"
        anomalies = getattr(result, "anomalies", [])
        created_at = getattr(result, "created_at", None)

        last_id: str | None = None
        for entity_type, entity_id in entities:
            # 构建实体相关的异常计数
            entity_anomaly_count = sum(
                1 for a in anomalies
                if entity_id in str(getattr(a, "documents", ""))
            )

            content = (
                f"{entity_type} {entity_id}：在 {analysis_type_val} 分析中"
                f"涉及 {entity_anomaly_count} 条异常"
            )

            # 从 supplier_kpis 补充指标
            for kpi in getattr(result, "supplier_kpis", []):
                sid = getattr(kpi, "vendor_id", "")
                if sid == entity_id:
                    kpi_values = getattr(kpi, "kpis", [])
                    if kpi_values:
                        metrics_str = ", ".join(
                            f"{getattr(k, 'name', '')}: {getattr(k, 'value', '')}"
                            for k in kpi_values[:5]
                        )
                        content += f"。关键指标：{metrics_str}"

            attrs: dict[str, Any] = {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "source_analysis_type": analysis_type_val,
                "analysis_date": created_at.isoformat() if created_at else None,
                "anomaly_count": entity_anomaly_count,
            }

            expires_at = self._repo.compute_expires_at(MemoryType.ENTITY_PROFILE)

            mid = self._repo.save(
                user_id=user_id,
                session_id=session_id,
                memory_type=MemoryType.ENTITY_PROFILE,
                content=content,
                metadata=attrs,
                entity_id=entity_id,
                expires_at=expires_at,
            )
            if mid:
                last_id = mid

        return last_id

    # ── analysis_insight 提取 ─────────────────────────────────

    def _extract_analysis_insight(
        self, result: Any, user_id: str, session_id: str,
    ) -> str | None:
        """对比历史同类分析，发现趋势/模式。

        只在有明确趋势时才写入（避免噪声）。
        至少需要 2 条历史同类记忆才能判断趋势。
        """
        analysis_type = getattr(result, "analysis_type", None)
        if analysis_type is None:
            return None
        analysis_type_val = analysis_type.value

        # 查询该用户的历史同类 entity_profile
        try:
            recent = self._repo.search(
                user_id=user_id,
                query=analysis_type_val,
                limit=10,
            )
        except Exception:  # noqa: BLE001
            return None

        same_type = [
            m for m in recent
            if (m.get("attrs") or {}).get("source_analysis_type") == analysis_type_val
        ]

        if len(same_type) < 2:
            return None

        # 检测异常数量趋势
        current_count = len(getattr(result, "anomalies", []))
        history_counts = [
            (m.get("attrs") or {}).get("anomaly_count", 0)
            for m in same_type[:5]
        ]

        trend = self._detect_simple_trend(history_counts, current_count)
        if trend is None:
            return None

        entities = self._collect_entities(result)
        entity_ids = [eid for _, eid in entities[:3]]

        content = (
            f"{analysis_type_val} 分析异常数量{trend['label']}："
            f"历史 {history_counts[:3]} → 当前 {current_count}"
        )
        if entity_ids:
            content += f"。涉及实体：{', '.join(entity_ids)}"

        expires_at = self._repo.compute_expires_at(MemoryType.ANALYSIS_INSIGHT)

        return self._repo.save(
            user_id=user_id,
            session_id=session_id,
            memory_type=MemoryType.ANALYSIS_INSIGHT,
            content=content,
            metadata={
                "pattern_type": "anomaly_trend",
                "related_entities": entity_ids,
                "related_analysis_types": [analysis_type_val],
                "trend_direction": trend["direction"],
                "observation_count": len(same_type) + 1,
            },
            expires_at=expires_at,
        )

    @staticmethod
    def _detect_simple_trend(
        history: list[int], current: int,
    ) -> dict[str, str] | None:
        """简单趋势检测：比较当前值与历史平均值。

        Returns:
            {"direction": "worsening|improving|stable", "label": "上升|下降|稳定"}
            或 None（无足够数据）。
        """
        if not history:
            return None

        avg = sum(history) / len(history)
        if avg == 0 and current == 0:
            return None

        # 变化幅度超过 30% 才视为趋势
        if avg > 0:
            change_rate = (current - avg) / avg
        else:
            change_rate = 1.0 if current > 0 else 0.0

        if change_rate > 0.3:
            return {"direction": "worsening", "label": "呈上升趋势"}
        if change_rate < -0.3:
            return {"direction": "improving", "label": "呈下降趋势"}
        return None  # stable — 不写入
