"""
报告生成 Agent。

DAG 执行的终点节点，汇总所有工具输出生成 Markdown 分析报告。
使用 LLM 做文本汇总，是 DAG 中唯一需要 LLM 调用的节点。
"""

from __future__ import annotations

from typing import Any

from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import Settings, get_settings
from core.logging_utils import get_logger
from modules.p2p.errors import (
    TRANSIENT_ERROR_CODES,
    ReportGenerationError,
    classify_llm_exception,
)

_logger = get_logger(__name__)


def _is_transient_llm_error(exc: BaseException) -> bool:
    """仅对瞬时网络类错误重试；业务错误 / 鉴权错误直接失败。"""
    return classify_llm_exception(exc) in TRANSIENT_ERROR_CODES

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
        self._llm = build_chat_model(self._settings.llm, disable_thinking=True)
        return self._llm

    async def generate(
        self,
        scenario: str,
        outputs: dict[str, str],
        output_mode_prompt: str = "",
    ) -> str:
        """根据 DAG 各节点输出生成 Markdown 报告。

        Args:
            scenario: 分析场景描述。
            outputs: DAG 各节点的输出 {output_key: result_json_str}。
            output_mode_prompt: 输出模式格式指令。

        Returns:
            Markdown 格式的分析报告。
        """
        from core.observability.middleware import record_span

        # 拼接所有工具输出
        outputs_parts: list[str] = []
        for key, value in outputs.items():
            if key == "report":
                continue
            truncated = value[:3000] if len(value) > 3000 else value
            outputs_parts.append(f"### {key}\n```json\n{truncated}\n```")

        outputs_text = "\n\n".join(outputs_parts) if outputs_parts else "（无数据输出）"

        prompt = _REPORT_PROMPT.format(
            scenario=scenario,
            outputs_text=outputs_text,
        )
        if output_mode_prompt:
            prompt += f"\n\n## 输出格式要求\n{output_mode_prompt}"

        with record_span("report", "generate_report") as span_attrs:
            span_attrs["scenario"] = scenario
            span_attrs["input_keys"] = [k for k in outputs if k != "report"]
            span_attrs["prompt_length"] = len(prompt)

            llm = self._ensure_llm()

            # 显式记录 model span（ReportAgent 不经过 LangChain 中间件）
            from core.observability.middleware import estimate_tokens

            model_name = getattr(llm, "model_name", None) or getattr(llm, "model", "unknown")

            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(3),
                    wait=wait_exponential(multiplier=1, min=1, max=4),
                    retry=retry_if_exception(_is_transient_llm_error),
                    reraise=True,
                ):
                    with attempt:
                        with record_span("model", str(model_name)) as model_attrs:
                            model_attrs["model"] = str(model_name)
                            model_attrs["input"] = prompt[:2000]
                            model_attrs["estimated_input_tokens"] = estimate_tokens(prompt)
                            if attempt.retry_state.attempt_number > 1:
                                model_attrs["retry_attempt"] = attempt.retry_state.attempt_number
                            response = await llm.ainvoke(prompt)
                            content: str = (
                                response.content
                                if hasattr(response, "content")
                                else str(response)
                            )
                            model_attrs["output"] = content[:2000]
                            usage = getattr(response, "usage_metadata", None) or getattr(
                                response, "response_metadata", None
                            )
                            if usage:
                                model_attrs["usage"] = (
                                    usage if isinstance(usage, dict) else str(usage)
                                )
            except RetryError as retry_err:
                # tenacity 仅在非 reraise 模式下抛 RetryError；这里 reraise=True
                # 进不到这条分支，留作兜底
                inner = retry_err.last_attempt.exception() or retry_err
                code = classify_llm_exception(inner)
                span_attrs["status"] = "error"
                span_attrs["error_code"] = code
                span_attrs["error"] = str(inner)
                _logger.warning(
                    "report generation exhausted retries: code=%s exc=%s", code, inner
                )
                raise ReportGenerationError(code, str(inner)) from inner
            except Exception as exc:
                code = classify_llm_exception(exc)
                span_attrs["status"] = "error"
                span_attrs["error_code"] = code
                span_attrs["error"] = str(exc)
                _logger.warning(
                    "report generation failed: code=%s exc=%s", code, exc
                )
                raise ReportGenerationError(code, str(exc)) from exc

            span_attrs["output_length"] = len(content)
            span_attrs["status"] = "ok"
            return content
