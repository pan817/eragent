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

## 时间上下文
当前日期：{current_date}（{timezone}）。所有"最近 N 天"/"本月"等相对时间以此为基准。

## 分析场景
{scenario}

## 工具输出数据
{outputs_text}

## 严重等级判定规则（客观阈值，不要主观评估）
- HIGH: 涉及金额 > ¥{high_amount} 的异常；或偏差比例超过容差的 {variance_mult} 倍以上
- MEDIUM: 已超容差但未达 HIGH 阈值的异常
- LOW: 疑似异常但未超容差，或数据不完整需人工复核

## 报告要求
1. 包含：摘要、关键发现、详细数据、建议措施
2. 对每项异常按上述规则标注严重等级（HIGH/MEDIUM/LOW）
3. 提供具体数据支撑（单据号、金额、偏差比例）
4. 给出可操作的改进建议

## 重要约束（必须遵守）
- **语言必须是中文**：所有段落、标题、要点、结论均使用简体中文；即使工具输出数据中含英文字段名或枚举值，正文叙述仍用中文
- 所有结论、数据、单据号、金额、供应商名称必须直接来源于上文"工具输出数据"；禁止推断、猜测或虚构未提供的具体数值
- 建议措施必须基于上文工具输出中已出现的具体异常或数据；不得引入未提及的供应商、未发生的事件或假想的系统改造项
- 看到 "[已截断]" 标记时，不要基于末尾内容做结论；必要时在报告中注明"数据过长已截断，结论仅覆盖前段"
- 如工具输出为空或数据不足以支撑某项结论，必须明确写"数据不足"或"无异常发现"，不得编造
- 本系统为分析只读系统，不会执行任何 ERP 写操作（付款、审批、工单创建、单据修改、邮件发送等）；改进动作一律以"建议人工处理"措辞表达，不要承诺或模拟执行
- 直接输出 Markdown 正文，不要添加前言或"好的，以下是..."之类的导语

请输出 Markdown 报告："""


class ReportAgent:
    """报告生成 Agent（轻量 LLM 调用）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._llm: Any = None

    def _ensure_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        from modules.p2p.model_factory import build_chat_model
        max_tok = self._settings.report.max_output_tokens
        self._llm = build_chat_model(
            self._settings.llm_fast,
            disable_thinking=True,
            max_tokens_override=max_tok if max_tok else None,
        )
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
        import time as _time

        _start = _time.monotonic()
        _logger.info(
            "report generate start: scenario=%s input_keys=%s",
            scenario,
            [k for k in outputs if k != "report"],
        )

        with record_span("report", "generate_report") as span_attrs:
            span_attrs["scenario"] = scenario
            span_attrs["input_keys"] = [k for k in outputs if k != "report"]

            # 子 span: 输出合并 + Prompt 构建 + LLM 懒加载初始化
            with record_span("report.prep", "prompt_build") as prep_attrs:
                outputs_parts: list[str] = []
                truncated_count = 0
                for key, value in outputs.items():
                    if key == "report":
                        continue
                    if len(value) > 3000:
                        truncated_count += 1
                        truncated = value[:3000] + "\n...[已截断，原始数据更长]"
                    else:
                        truncated = value
                    outputs_parts.append(f"### {key}\n```json\n{truncated}\n```")

                outputs_text = (
                    "\n\n".join(outputs_parts) if outputs_parts else "（无数据输出）"
                )

                from core.time_utils import get_timezone_name, now_cn

                anomaly_cfg = self._settings.p2p.anomaly_severity
                prompt = _REPORT_PROMPT.format(
                    scenario=scenario,
                    outputs_text=outputs_text,
                    high_amount=f"{int(anomaly_cfg.high_amount_threshold):,}",
                    variance_mult=f"{anomaly_cfg.variance_high_multiplier:g}",
                    current_date=now_cn().strftime("%Y-%m-%d"),
                    timezone=get_timezone_name(),
                )
                if output_mode_prompt:
                    # 输出模式（brief / table 等）优先级高于上文"报告要求"的结构/字数规定；
                    # 冲突时以此节为准，避免默认 4 段结构与 brief 500 字限制相互拉扯。
                    prompt += (
                        f"\n\n## 输出格式要求（优先级高于上文\"报告要求\"）\n"
                        f"{output_mode_prompt}\n"
                        f"若与上文\"报告要求\"的结构或字数规定冲突，以本节为准。"
                    )

                import hashlib

                prompt_hash = hashlib.md5(prompt.encode("utf-8")).hexdigest()[:12]
                prep_attrs["input_keys_count"] = len(outputs_parts)
                prep_attrs["outputs_total_chars"] = sum(len(v) for v in outputs.values())
                prep_attrs["truncated_outputs"] = truncated_count
                prep_attrs["prompt_length"] = len(prompt)
                prep_attrs["prompt_hash"] = prompt_hash
                prep_attrs["has_output_mode_prompt"] = bool(output_mode_prompt)
                span_attrs["prompt_hash"] = prompt_hash

                llm = self._ensure_llm()
                prep_attrs["llm_ready"] = True

            span_attrs["prompt_length"] = len(prompt)

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
            _logger.info(
                "report generate done: scenario=%s output_length=%d "
                "prompt_length=%d duration=%.1fms",
                scenario, len(content), len(prompt),
                (_time.monotonic() - _start) * 1000,
            )
            return content
