"""LLM 流式输出共享工具。

Phase 1 (ReportAgent) 与 Phase 2 (P2PAgent ReAct) 的流式路径需要相同的
token 文本提取、<think> 推理标签剥离、ChunkEvent 推送逻辑。本模块集中维护
这些工具，避免两处重复实现漂移。

约定：

- ``extract_chunk_text``：纯函数，从 LangChain AIMessageChunk 中抽取增量文本
- ``strip_think_tags``：纯函数，对完整文本做 ``<think>...</think>`` 清理
- ``ThinkTagFilter``：有状态过滤器，流式期按 chunk 增量喂入、输出可推送文本
- ``publish_chunk_event``：统一的 ChunkEvent 推送入口（ephemeral=True）
"""

from __future__ import annotations

import re
from typing import Any

from core.time_utils import now_cn

_TAG_OPEN = "<think>"
_TAG_CLOSE = "</think>"
_MAX_TAG_LEN = max(len(_TAG_OPEN), len(_TAG_CLOSE))


def extract_chunk_text(chunk: Any) -> str:
    """从 ``AIMessageChunk`` 中提取增量文本，兼容多种模型返回结构。

    优先级：

    1. ``chunk.content`` 是非空 ``str`` → 直接使用（OpenAI / Qwen 主模型）
    2. ``chunk.content`` 是 ``list[dict]`` → 拼接所有 text block 的 ``text``
       （LangChain 对多模态 / reasoning 模型用 content-blocks 结构）
    3. ``chunk.additional_kwargs["reasoning_content"]`` 非空 → 作为兜底

    #3 专门处理 Qwen3 系列的**已知 Bug**：当 ``enable_thinking=False`` 与
    ``stream=True`` 同时生效（Dashscope OpenAI 兼容接口、vLLM、SGLang 后端
    均中招），模型把最终答案错误地落在 ``reasoning_content`` 字段而非
    ``content``；此时 ``content`` 是空串，若只读 content 会得到 0 长度输出。
    参考：sgl-project/sglang#5874、vllm-project/vllm#38894。
    """
    raw = getattr(chunk, "content", None)
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str) and text:
                    parts.append(text)
        if parts:
            return "".join(parts)
    extra = getattr(chunk, "additional_kwargs", None) or {}
    reasoning = extra.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return reasoning
    return ""


def strip_think_tags(text: str) -> str:
    """从最终文本中剥离 ``<think>...</think>`` 推理标签及其内容。

    - 支持多个 ``<think>`` 块
    - 支持嵌套空白、换行
    - 标签未闭合（截断）时剥掉从 ``<think>`` 到末尾的全部内容
    - 早退优化：不含 ``<think>`` 直接返回原文
    """
    if "<think>" not in text:
        return text
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text)
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned)
    return cleaned.strip()


class ThinkTagFilter:
    """流式 ``<think>`` 标签过滤器。

    按 chunk 增量喂入文本，维护"是否处于 suppress 状态"与"边界残留"，
    输出可推送给前端的可见文本。

    使用方式::

        filter = ThinkTagFilter()
        async for chunk in stream:
            text = extract_chunk_text(chunk)
            visible, raw = filter.feed(text)
            # visible: 可推送给前端的文本（已剔除 <think>...</think>）
            # raw:     原始文本（含 <think> 标签），调用方累加到 accumulated
        tail_visible, tail_raw = filter.flush()

    关键不变量：

    - 同一次 ``feed`` 调用可能同时产出 visible 与新的 boundary_buf；
      boundary_buf 不会出现在 visible 中，会在后续 feed 或 flush 中处理。
    - ``suppressing`` 为 True 时，feed 只会返回空 visible（内容全部被 suppress）。
    """

    def __init__(self) -> None:
        self._suppressing = False
        self._boundary_buf = ""

    @property
    def suppressing(self) -> bool:
        return self._suppressing

    def feed(self, text: str) -> tuple[str, str]:
        """喂入新增量文本。

        Returns:
            ``(visible, raw)``：

            - ``visible``：可推送给前端的部分（已剔除 <think>...</think>）
            - ``raw``：本次处理覆盖的原始文本（含 <think> 标签），调用方累加
        """
        if not text:
            return "", ""

        visible_parts: list[str] = []
        raw_parts: list[str] = []

        # 拼接上一轮边界残留
        if self._boundary_buf:
            text = self._boundary_buf + text
            self._boundary_buf = ""

        cursor = 0
        while cursor < len(text):
            if self._suppressing:
                close_pos = text.find(_TAG_CLOSE, cursor)
                if close_pos >= 0:
                    # 闭合：跳过 </think> 本身，raw 里留着（末尾 strip 会清）
                    skip_end = close_pos + len(_TAG_CLOSE)
                    raw_parts.append(text[cursor:skip_end])
                    cursor = skip_end
                    self._suppressing = False
                else:
                    # 未闭合：整段 suppress，留边界尾部
                    raw_parts.append(text[cursor:])
                    tail_len = min(_MAX_TAG_LEN - 1, len(text) - cursor)
                    self._boundary_buf = text[-tail_len:] if tail_len > 0 else ""
                    cursor = len(text)
            else:
                open_pos = text.find(_TAG_OPEN, cursor)
                if open_pos >= 0:
                    # <think> 之前的内容是正常文本
                    normal = text[cursor:open_pos]
                    if normal:
                        visible_parts.append(normal)
                        raw_parts.append(normal)
                    # <think> 本身进 raw（末尾 strip 会清）
                    raw_parts.append(_TAG_OPEN)
                    cursor = open_pos + len(_TAG_OPEN)
                    self._suppressing = True
                else:
                    # 无标签：全部是正常文本，但保留尾部作为边界缓冲
                    # （可能是 "<thi" 这种半截开始标签）
                    safe_end = len(text) - (_MAX_TAG_LEN - 1)
                    if safe_end > cursor:
                        normal = text[cursor:safe_end]
                        visible_parts.append(normal)
                        raw_parts.append(normal)
                        self._boundary_buf = text[safe_end:]
                    else:
                        # 文本太短，全部留到边界缓冲
                        self._boundary_buf = text[cursor:]
                    cursor = len(text)

        return "".join(visible_parts), "".join(raw_parts)

    def flush(self) -> tuple[str, str]:
        """流结束后的兜底：处理边界缓冲。

        Returns:
            ``(visible, raw)``：若 boundary_buf 非空且非 suppress 状态，
            原样返回（正常文本）；否则返回 ``("", "")``。
        """
        if self._boundary_buf and not self._suppressing:
            tail = self._boundary_buf
            self._boundary_buf = ""
            return tail, tail
        # suppress 状态下的边界残留丢弃（等价于 strip_think_tags 兜底）
        self._boundary_buf = ""
        return "", ""


def publish_chunk_event(
    bus: Any,
    *,
    trace_id: str,
    node: str,
    message_id: str,
    delta: str,
    index: int,
    eos: bool = False,
) -> None:
    """统一的 ChunkEvent 推送入口。

    约束：

    - ``seq`` 固定 0（不进 ring buffer、不参与 Last-Event-ID 重放）
    - ``ephemeral=True``（配合 ``seq=0``，让 EventBus 跳过持久化）
    - 空 ``delta`` 且非 eos 时不推送（避免冗余事件）

    调用方（Phase 1 / Phase 2）**不应**自行构造 dict，统一经此函数以保证协议一致。
    """
    if bus is None:
        return
    if not delta and not eos:
        return
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
            "index": index,
            "eos": eos,
        },
        ephemeral=True,
    )
