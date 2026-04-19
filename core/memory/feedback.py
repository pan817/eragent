"""FeedbackDetector — 用户反馈检测 + LLM 提取。

两阶段设计：
1. 关键词预筛（零成本）——快速排除明显的分析请求
2. LLM 精确分类（仅在预筛通过时调用）
"""

from __future__ import annotations

import json
import logging
from typing import Any

_logger = logging.getLogger(__name__)

# ── Prompt ────────────────────────────────────────────────────────

FEEDBACK_EXTRACT_PROMPT = """\
你是 ERP 采购分析系统的反馈提取器。
用户刚才收到了一份分析报告，现在发送了新的消息。

用户消息：{query}
最近分析摘要：{context_summary}

请判断用户消息是否包含以下任一类型的反馈：

1. correction — 对分析结果的修正（指出错误、补充信息）
2. user_preference — 分析偏好表达（输出格式、关注阈值等）
3. domain_fact — 业务事实告知（企业规则、阈值、例外情况）
4. none — 不是反馈，是追问或新的分析请求

输出严格 JSON 格式（不要输出其他内容）：
{{"type": "correction|user_preference|domain_fact|none", "content": "提取的记忆内容", "related_entities": {{"po_number": "", "supplier_id": ""}}, "confidence": 0.0}}

规则：
- 如果 type=none 或 confidence<0.6，content 必须为空字符串
- related_entities 中只填用户消息中明确提到的实体，没有则留空串
- content 用自然语言描述，包含完整上下文（谁、什么、为什么）
"""


class FeedbackDetector:
    """检测用户输入中的反馈/纠正/偏好信号。"""

    # 预筛关键词
    _CORRECTION_SIGNALS: set[str] = {
        "不对", "不是", "错了", "不算", "不应该", "其实是", "实际上",
        "是因为", "原因是", "不要标记", "误报", "不准确", "搞错",
    }
    _PREFERENCE_SIGNALS: set[str] = {
        "以后", "默认", "总是", "每次", "偏好", "喜欢", "习惯",
        "用表格", "用图表", "只看", "只关心", "关注",
    }
    _FACT_SIGNALS: set[str] = {
        "我们公司", "我们的", "企业规定", "标准是", "阈值是",
        "容差", "规则是", "政策是", "独家供应商",
    }

    def detect_signal(self, query: str) -> str | None:
        """关键词预筛：返回 'correction' / 'preference' / 'fact' / None。

        耗时 <1ms，每次用户请求都可调用。
        """
        q = query.lower()
        signal: str | None = None
        if any(w in q for w in self._CORRECTION_SIGNALS):
            signal = "correction"
        elif any(w in q for w in self._PREFERENCE_SIGNALS):
            signal = "preference"
        elif any(w in q for w in self._FACT_SIGNALS):
            signal = "fact"

        if signal:
            try:
                from core.observability.tracing import record_span

                with record_span("memory", "memory.feedback.detect", signal_type=signal):
                    pass
            except Exception:  # noqa: BLE001
                pass

        return signal

    async def extract(
        self,
        query: str,
        session_context: dict[str, Any],
        signal_type: str,
        llm_fast: Any,
    ) -> dict[str, Any] | None:
        """LLM 精确提取反馈内容。仅在 detect_signal 命中时调用。

        Args:
            query: 用户原始消息
            session_context: 会话上下文（含 context_summary）
            signal_type: 预筛信号类型（用于日志，不影响 LLM 判断）
            llm_fast: 轻量 LLM 实例

        Returns:
            提取结果 dict 或 None（LLM 判定非反馈或解析失败）。
        """
        context_summary = session_context.get("context_summary", "")

        prompt = FEEDBACK_EXTRACT_PROMPT.format(
            query=query,
            context_summary=context_summary[:1000] if context_summary else "(无)",
        )

        try:
            from langchain_core.messages import HumanMessage

            response = await llm_fast.ainvoke([HumanMessage(content=prompt)])
            raw = getattr(response, "content", "")
            return self._parse_response(raw)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "feedback LLM extract failed: signal=%s error=%s",
                signal_type, exc,
            )
            return None

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any] | None:
        """解析 LLM JSON 响应。解析失败返回 None。"""
        text = raw.strip()
        # 处理 markdown code block 包裹
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            ).strip()

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            _logger.warning("feedback LLM response not valid JSON: %s", text[:200])
            return None

        fb_type = data.get("type", "none")
        if fb_type == "none":
            return None

        content = data.get("content", "").strip()
        if not content:
            return None

        return {
            "type": fb_type,
            "content": content,
            "related_entities": data.get("related_entities", {}),
            "confidence": float(data.get("confidence", 0.0)),
        }
