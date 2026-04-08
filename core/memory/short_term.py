"""短期记忆：单个会话内的多轮对话上下文管理。"""

from __future__ import annotations

from collections.abc import Callable


class ShortTermMemory:
    """管理单个会话的短期记忆（多轮对话上下文）。

    内部维护一个有序消息列表和可选的摘要文本。当消息数量超过
    ``summary_threshold`` 时，可通过外部提供的摘要函数将早期消息
    压缩为一段摘要，从而在保持上下文连贯性的同时控制 token 消耗。

    Attributes:
        max_messages: 保留的最大原始消息数量。
        summary_threshold: 触发压缩的消息数量阈值。
        messages: 当前会话的消息列表。
        summary: 压缩后的早期对话摘要文本。
    """

    def __init__(
        self,
        max_messages: int = 20,
        summary_threshold: int = 15,
    ) -> None:
        self.max_messages: int = max_messages
        self.summary_threshold: int = summary_threshold
        self.messages: list[dict[str, str]] = []
        self.summary: str = ""

    def add_message(self, role: str, content: str) -> None:
        """向对话历史中追加一条消息。"""
        self.messages.append({"role": role, "content": content})

    def get_context(self) -> list[dict[str, str]]:
        """返回当前上下文（包含摘要 system 消息和近期消息）。"""
        context: list[dict[str, str]] = []
        if self.summary:
            context.append({
                "role": "system",
                "content": f"以下是早期对话的摘要：\n{self.summary}",
            })
        context.extend(self.messages)
        return context

    def needs_compression(self) -> bool:
        """判断当前消息数量是否已达到压缩阈值。"""
        return len(self.messages) >= self.summary_threshold

    def compress(self, summarizer_fn: Callable[[list[dict[str, str]]], str]) -> None:
        """将早期消息压缩为摘要，仅保留最近的消息。

        保留数量为 ``max(len/2, 5)``。已有摘要会与新摘要拼接保留。
        """
        keep_count: int = max(len(self.messages) // 2, 5)
        if keep_count >= len(self.messages):
            return

        early_messages: list[dict[str, str]] = self.messages[:-keep_count]
        new_summary: str = summarizer_fn(early_messages)

        self.summary = (
            f"{self.summary}\n\n{new_summary}" if self.summary else new_summary
        )
        self.messages = self.messages[-keep_count:]

    def clear(self) -> None:
        """清空所有消息和摘要。"""
        self.messages.clear()
        self.summary = ""
