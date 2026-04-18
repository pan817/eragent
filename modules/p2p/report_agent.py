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
- 如工具输出为空或数据不足以支撑某项结论，必须明确写"数据不足"或"无异常发现"，不得编造
- 本系统为分析只读系统，不会执行任何 ERP 写操作（付款、审批、工单创建、单据修改、邮件发送等）；改进动作一律以"建议人工处理"措辞表达，不要承诺或模拟执行
- 直接输出 Markdown 正文，不要添加前言或"好的，以下是..."之类的导语
- 禁止输出 <think>、</think> 或任何 XML 推理标签；不要输出推理过程，只输出最终报告

请输出 Markdown 报告："""


class ReportAgent:
    """报告生成 Agent（轻量 LLM 调用）。"""

    # 流式 micro-batching 参数：每累计 ``_STREAM_FLUSH_CHARS`` 字符或每
    # ``_STREAM_FLUSH_INTERVAL`` 秒（以先到者为准）往 EventBus flush 一次，
    # 目的是把 token 级事件聚合成 ~20 Hz 的 chunk 事件，兼顾体验与带宽。
    _STREAM_FLUSH_CHARS: int = 16
    _STREAM_FLUSH_INTERVAL: float = 0.05

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._llm: Any = None

    def _ensure_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        from core.llm.model_factory import build_chat_model
        max_tok = self._settings.report.max_output_tokens
        self._llm = build_chat_model(
            self._settings.llm_fast,
            disable_thinking=True,
            max_tokens_override=max_tok if max_tok else None,
        )
        return self._llm

    @staticmethod
    def _extract_chunk_text(chunk: Any) -> str:
        """兼容薄壳，委托给 ``core.tasks.stream_utils.extract_chunk_text``。"""
        from core.tasks.stream_utils import extract_chunk_text
        return extract_chunk_text(chunk)

    @staticmethod
    def _strip_think_tags(text: str) -> str:
        """兼容薄壳，委托给 ``core.tasks.stream_utils.strip_think_tags``。"""
        from core.tasks.stream_utils import strip_think_tags
        return strip_think_tags(text)

    async def _astream_with_publish(
        self,
        llm: Any,
        prompt: str,
        trace_id: str,
        message_id: str,
        *,
        node: str = "report",
    ) -> tuple[str, Any]:
        """调用 ``llm.astream(prompt)``，按 micro-batch 推送 chunk 事件。

        **Phase 2 流式状态机**：检测到 ``<think>`` 开始标签后进入 suppress
        状态，后续 chunk 文本仅累加到内部 buffer（用于检测 ``</think>``
        闭合），不 flush 到 EventBus——前端看不到推理过程。检测到
        ``</think>`` 后恢复正常 flush。

        **Phase 1 兜底**：即使状态机遗漏（例如标签跨 chunk 边界被拆散），
        末尾 ``_strip_think_tags`` 仍会清除残留。两层联动保证：
        流式过程中不显示 thinking + 最终报告文本干净。

        Args:
            llm: 已构造的 ChatOpenAI 实例（``streaming=True``）。
            prompt: 发给 LLM 的完整 prompt。
            trace_id: 当前异步任务 ID，用于 EventBus 分发。
            message_id: 前端 pending 气泡的 assistant_message_id（字符串）。
            node: ChunkEvent.node 字段，Phase 1 固定为 "report"。

        Returns:
            ``(完整文本, usage_metadata)``；usage_metadata 来自最后一个非空 chunk。
        """
        from core.tasks.events import get_event_bus
        from core.time_utils import now_cn
        import time as _time

        bus = get_event_bus()
        accumulated: list[str] = []
        pending: list[str] = []
        last_flush_ts = _time.monotonic()
        chunk_index = 0
        final_meta: Any = None

        # ── Phase 2 流式状态机：suppress <think> 内容不推给前端 ──
        # suppressing=True 期间 text 只进 accumulated（用于末尾 strip），
        # 不进 pending（不 flush 到 EventBus）。
        suppressing = False
        # 边界缓冲：标签可能被拆散在连续 chunk 的边界上，如 "<thi" + "nk>"。
        # 用一个短 buffer 记住最近未匹配的尾部片段，下一个 chunk 拼接后重新检测。
        _TAG_OPEN = "<think>"
        _TAG_CLOSE = "</think>"
        _MAX_TAG_LEN = max(len(_TAG_OPEN), len(_TAG_CLOSE))
        boundary_buf = ""

        def _pending_chars() -> int:
            return sum(len(s) for s in pending)

        def _flush(eos: bool = False) -> None:
            """把 pending buffer flush 成一条 chunk 事件推送。"""
            nonlocal chunk_index, last_flush_ts
            delta = "".join(pending)
            pending.clear()
            # 空 delta 且非 eos 不发，避免冗余事件
            if bus is not None and (delta or eos):
                bus.publish(
                    trace_id,
                    {
                        "type": "chunk",
                        "trace_id": trace_id,
                        "ts": now_cn().isoformat(),
                        "seq": 0,
                        "node": node,
                        "message_id": message_id,
                        "delta": delta,
                        "index": chunk_index,
                        "eos": eos,
                    },
                    ephemeral=True,
                )
                chunk_index += 1
            last_flush_ts = _time.monotonic()

        def _process_text(text: str) -> None:
            """处理提取到的文本：检测 <think> 标签、按状态分流到 pending/accumulated。"""
            nonlocal suppressing, boundary_buf

            # 拼接上一轮的边界残留
            if boundary_buf:
                text = boundary_buf + text
                boundary_buf = ""

            cursor = 0
            while cursor < len(text):
                if suppressing:
                    # 找 </think> 闭合
                    close_pos = text.find(_TAG_CLOSE, cursor)
                    if close_pos >= 0:
                        # 闭合：跳过 </think> 本身，accumulated 里留着（末尾 strip 会清）
                        skip_end = close_pos + len(_TAG_CLOSE)
                        accumulated.append(text[cursor:skip_end])
                        cursor = skip_end
                        suppressing = False
                    else:
                        # 未闭合：整段 suppress，留边界尾部
                        accumulated.append(text[cursor:])
                        # 保留尾部可能是 "</thin" 这种半截标签
                        tail_len = min(_MAX_TAG_LEN - 1, len(text) - cursor)
                        boundary_buf = text[-tail_len:] if tail_len > 0 else ""
                        cursor = len(text)
                else:
                    # 找 <think> 开始
                    open_pos = text.find(_TAG_OPEN, cursor)
                    if open_pos >= 0:
                        # <think> 之前的内容是正常文本
                        normal = text[cursor:open_pos]
                        if normal:
                            pending.append(normal)
                            accumulated.append(normal)
                        # <think> 本身进 accumulated（末尾 strip 会清）
                        accumulated.append(_TAG_OPEN)
                        cursor = open_pos + len(_TAG_OPEN)
                        suppressing = True
                    else:
                        # 无标签：全部是正常文本，但保留尾部作为边界缓冲
                        # （可能是 "<thi" 这种半截开始标签）
                        safe_end = len(text) - (_MAX_TAG_LEN - 1)
                        if safe_end > cursor:
                            normal = text[cursor:safe_end]
                            pending.append(normal)
                            accumulated.append(normal)
                            boundary_buf = text[safe_end:]
                        else:
                            # 文本太短，全部留到边界缓冲
                            boundary_buf = text[cursor:]
                        cursor = len(text)

        async for chunk in llm.astream(prompt):
            text = self._extract_chunk_text(chunk)
            if text:
                _process_text(text)
            meta = getattr(chunk, "usage_metadata", None)
            if meta:
                final_meta = meta
            # 达到字符阈值或时间阈值即 flush（仅 pending 非空 + 非 suppress 时有效）
            if (
                not suppressing
                and (
                    _pending_chars() >= self._STREAM_FLUSH_CHARS
                    or (_time.monotonic() - last_flush_ts) >= self._STREAM_FLUSH_INTERVAL
                )
            ):
                _flush()

        # 流结束后如果 boundary_buf 还有残留正常文本（非 suppress 状态），flush 出去
        if boundary_buf and not suppressing:
            pending.append(boundary_buf)
            accumulated.append(boundary_buf)
            boundary_buf = ""
        # 末尾兜底 flush（空 delta 也要带 eos=True 让前端结束累加状态）
        _flush(eos=True)
        full_text = self._strip_think_tags("".join(accumulated))
        if not full_text:
            # 流式跑完但没拿到任何文本：常见于 Qwen3 / 部分 provider 的兼容性异常。
            # 抛 EMPTY_RESPONSE 让上层 tenacity 按 transient 规则决定是否重试；
            # 即使不重试也比静默写一份空 report 好——前端能看到明确的 error 事件。
            from modules.p2p.errors import ReportGenerationError

            raise ReportGenerationError(
                "EMPTY_RESPONSE",
                "LLM 流式返回为空（content / reasoning_content 均无文本）；"
                "常见原因：Qwen3 系列 enable_thinking=False + stream=True "
                "的兼容性 bug。可关闭 llm_fast.streaming_enabled 回退到 ainvoke。",
            )
        return full_text, final_meta

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
                # —— 1) outputs 合并/截断 ——
                # 两级预算：
                #   a. 总预算 = context_window * outputs_context_max_tokens_pct%
                #             - max_output_tokens - outputs_template_overhead_tokens
                #      用于兜住所有 outputs 合计占用，不让单次报告生成把窗口打爆。
                #   b. per-key 软上限 = min(总预算均摊, outputs_per_key_max_chars)
                #      保证多 key 场景下任何单个 key 都不会独占预算。
                # 预算换算回字符数使用 llm_fast.token_estimate_ratio（字符/token）。
                with record_span("report.prep", "merge_outputs") as merge_attrs:
                    from core.observability.middleware import estimate_tokens

                    report_cfg = self._settings.report
                    llm_fast_cfg = self._settings.llm_fast
                    ratio = max(0.5, float(llm_fast_cfg.token_estimate_ratio))

                    active_keys = [k for k in outputs if k != "report"]
                    key_count = max(1, len(active_keys))

                    if report_cfg.outputs_context_max_tokens_pct > 0:
                        total_budget_tokens = int(
                            llm_fast_cfg.context_window
                            * report_cfg.outputs_context_max_tokens_pct
                            / 100
                            - report_cfg.max_output_tokens
                            - report_cfg.outputs_template_overhead_tokens
                        )
                        total_budget_tokens = max(1000, total_budget_tokens)
                        total_budget_chars = int(total_budget_tokens * ratio)
                        per_key_budget_chars = total_budget_chars // key_count
                    else:
                        total_budget_chars = 0  # 关闭预算制
                        per_key_budget_chars = 10**9  # 不设上限

                    per_key_hard_limit = report_cfg.outputs_per_key_max_chars
                    if per_key_hard_limit > 0:
                        per_key_limit = min(per_key_budget_chars, per_key_hard_limit)
                    else:
                        per_key_limit = per_key_budget_chars

                    outputs_parts: list[str] = []
                    truncated_count = 0
                    for key in active_keys:
                        value = outputs[key]
                        if per_key_limit > 0 and len(value) > per_key_limit:
                            truncated_count += 1
                            clipped = value[:per_key_limit]
                        else:
                            clipped = value
                        outputs_parts.append(f"### {key}\n```json\n{clipped}\n```")

                    outputs_text = (
                        "\n\n".join(outputs_parts) if outputs_parts else "（无数据输出）"
                    )

                    # 兜底：合并后仍超总预算（连接符 + 小头部叠加），从最长的 part
                    # 尾部继续切，保证总长度不超 total_budget_chars。
                    if total_budget_chars > 0 and len(outputs_text) > total_budget_chars:
                        overflow = len(outputs_text) - total_budget_chars
                        # 找最长 part 并切
                        longest_idx = max(
                            range(len(outputs_parts)),
                            key=lambda i: len(outputs_parts[i]),
                        )
                        longest = outputs_parts[longest_idx]
                        cut = max(0, len(longest) - overflow)
                        outputs_parts[longest_idx] = longest[:cut]
                        truncated_count += 1
                        outputs_text = "\n\n".join(outputs_parts)

                    merge_attrs["input_keys_count"] = len(outputs_parts)
                    merge_attrs["outputs_total_chars"] = sum(
                        len(v) for v in outputs.values()
                    )
                    merge_attrs["truncated_outputs"] = truncated_count
                    merge_attrs["outputs_text_chars"] = len(outputs_text)
                    merge_attrs["outputs_text_tokens_est"] = estimate_tokens(outputs_text)
                    merge_attrs["total_budget_chars"] = total_budget_chars
                    merge_attrs["per_key_limit_chars"] = per_key_limit

                # —— 2) Prompt 模板渲染 ——
                with record_span("report.prep", "format_prompt") as fmt_attrs:
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
                    fmt_attrs["prompt_length"] = len(prompt)
                    fmt_attrs["has_output_mode_prompt"] = bool(output_mode_prompt)

                # —— 3) 哈希指纹 ——
                with record_span("report.prep", "hash_prompt") as hash_attrs:
                    import hashlib

                    prompt_hash = hashlib.md5(prompt.encode("utf-8")).hexdigest()[:12]
                    hash_attrs["prompt_length"] = len(prompt)
                    hash_attrs["prompt_hash"] = prompt_hash

                # —— 4) LLM 懒加载（疑点：tiktoken / ChatOpenAI 构造）——
                with record_span("report.prep", "ensure_llm") as llm_attrs:
                    llm_attrs["cached"] = self._llm is not None
                    llm = self._ensure_llm()
                    llm_attrs["llm_ready"] = True

                prep_attrs["input_keys_count"] = len(outputs_parts)
                prep_attrs["outputs_total_chars"] = sum(len(v) for v in outputs.values())
                prep_attrs["truncated_outputs"] = truncated_count
                prep_attrs["prompt_length"] = len(prompt)
                prep_attrs["prompt_hash"] = prompt_hash
                prep_attrs["has_output_mode_prompt"] = bool(output_mode_prompt)
                span_attrs["prompt_hash"] = prompt_hash

            span_attrs["prompt_length"] = len(prompt)

            # 显式记录 model span（ReportAgent 不经过 LangChain 中间件）
            from core.observability.middleware import estimate_tokens

            model_name = getattr(llm, "model_name", None) or getattr(llm, "model", "unknown")

            # 判定是否走流式路径：
            #   - 配置开关 llm_fast.streaming_enabled 打开（可一键降级）
            #   - 当前上下文能拿到 trace_id（EventBus 按 trace_id 分发事件）
            # message_id 不作为流式的开关条件：缺失时（例如 auto_persist=False
            # 或 chat_repo 未配置）用 trace_id 作为 fallback 填充 ChunkEvent.message_id，
            # 保证"只要有 trace_id 就能流式"。前端有 auto_persist 时按 assistant_message_id
            # 绑定气泡，没有时按 trace_id 绑定。
            from core.observability.middleware import _current_trace
            from core.tasks.context import get_current_message_id
            from core.tasks.events import get_event_bus

            _trace_ctx = _current_trace.get()
            _trace_id = _trace_ctx.trace_id if _trace_ctx is not None else None
            _message_id = get_current_message_id() or _trace_id
            _bus_ready = get_event_bus() is not None
            streaming_on = bool(
                self._settings.llm_fast.streaming_enabled
                and _trace_id
                and _bus_ready
            )

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
                            model_attrs["streaming"] = streaming_on
                            if attempt.retry_state.attempt_number > 1:
                                model_attrs["retry_attempt"] = attempt.retry_state.attempt_number
                            if streaming_on:
                                content, usage = await self._astream_with_publish(
                                    llm,
                                    prompt,
                                    trace_id=_trace_id,  # type: ignore[arg-type]
                                    message_id=_message_id,  # type: ignore[arg-type]
                                    node="report",
                                )
                            else:
                                response = await llm.ainvoke(prompt)
                                content = (
                                    response.content
                                    if hasattr(response, "content")
                                    else str(response)
                                )
                                usage = getattr(response, "usage_metadata", None) or getattr(
                                    response, "response_metadata", None
                                )
                            model_attrs["output"] = content[:2000]
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

            # 统一剥离 <think> 标签（覆盖流式 + ainvoke 两条路径）。
            # 流式分支在 _astream_with_publish 内已 strip 过一次，
            # 此处再 strip 是幂等的（两次结果相同），保证 ainvoke 路径也干净。
            content = self._strip_think_tags(content)

            span_attrs["output_length"] = len(content)
            span_attrs["status"] = "ok"
            _logger.info(
                "report generate done: scenario=%s output_length=%d "
                "prompt_length=%d duration=%.1fms",
                scenario, len(content), len(prompt),
                (_time.monotonic() - _start) * 1000,
            )
            return content
