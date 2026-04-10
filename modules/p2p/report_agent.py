"""
报告生成 Agent。

DAG 执行的终点节点，汇总所有工具输出生成 Markdown 分析报告。
使用 LLM 做文本汇总，是 DAG 中唯一需要 LLM 调用的节点。
"""

from __future__ import annotations

from typing import Any

from config.settings import Settings, get_settings
from core.logging_utils import get_logger

_logger = get_logger(__name__)

_REPORT_PROMPT = """你是 ERP 采购分析系统的报告生成器。根据以下分析工具的输出，生成一份结构化的 Markdown 分析报告。

## 分析场景
{scenario}

## 工具输出数据
{outputs_text}

## 报告要求
1. 使用中文
2. 包含：摘要、关键发现、详细数据、建议措施
3. 对异常标注严重等级（HIGH/MEDIUM/LOW）
4. 提供具体数据支撑（单据号、金额、偏差比例）
5. 给出可操作的改进建议
6. 如果数据为空或工具返回空结果，说明无异常发现

请直接输出 Markdown 报告："""


class ReportAgent:
    """报告生成 Agent（轻量 LLM 调用）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._llm: Any = None

    def _ensure_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        from modules.p2p.model_factory import build_chat_model
        self._llm = build_chat_model(self._settings.llm)
        return self._llm

    async def generate(
        self,
        scenario: str,
        outputs: dict[str, str],
    ) -> str:
        """根据 DAG 各节点输出生成 Markdown 报告。

        Args:
            scenario: 分析场景描述。
            outputs: DAG 各节点的输出 {output_key: result_json_str}。

        Returns:
            Markdown 格式的分析报告。
        """
        # 拼接所有工具输出
        outputs_parts: list[str] = []
        for key, value in outputs.items():
            if key == "report":
                continue  # 跳过自身
            truncated = value[:3000] if len(value) > 3000 else value
            outputs_parts.append(f"### {key}\n```json\n{truncated}\n```")

        outputs_text = "\n\n".join(outputs_parts) if outputs_parts else "（无数据输出）"

        prompt = _REPORT_PROMPT.format(
            scenario=scenario,
            outputs_text=outputs_text,
        )

        try:
            llm = self._ensure_llm()
            response = await llm.ainvoke(prompt)
            content: str = response.content if hasattr(response, "content") else str(response)
            return content
        except Exception as exc:
            _logger.warning("report generation failed: %s", exc)
            return f"# {scenario}\n\n> 报告生成失败：{exc}\n\n## 原始数据\n\n{outputs_text}"
