"""记忆管理模块单元测试。"""

from __future__ import annotations

from core.memory import ShortTermMemory


class TestShortTermMemory:
    """短期记忆测试。"""

    def test_add_message(self) -> None:
        """添加消息后应在消息列表中。"""
        mem = ShortTermMemory(max_messages=20, summary_threshold=15)
        mem.add_message("user", "你好")
        mem.add_message("assistant", "你好！有什么可以帮你的？")
        ctx = mem.get_context()
        assert len(ctx) == 2
        assert ctx[0]["role"] == "user"
        assert ctx[1]["role"] == "assistant"

    def test_needs_compression_false(self) -> None:
        """消息数未超阈值时不需要压缩。"""
        mem = ShortTermMemory(max_messages=20, summary_threshold=15)
        for i in range(10):
            mem.add_message("user", f"消息 {i}")
        assert not mem.needs_compression()

    def test_needs_compression_true(self) -> None:
        """消息数超过阈值时需要压缩。"""
        mem = ShortTermMemory(max_messages=20, summary_threshold=5)
        for i in range(6):
            mem.add_message("user", f"消息 {i}")
        assert mem.needs_compression()

    def test_compress(self) -> None:
        """压缩后应有摘要，消息数应减少。"""
        mem = ShortTermMemory(max_messages=20, summary_threshold=5)
        for i in range(10):
            mem.add_message("user", f"消息 {i}")

        def fake_summarizer(messages: list[dict]) -> str:
            return f"摘要（{len(messages)} 条消息）"

        mem.compress(fake_summarizer)
        ctx = mem.get_context()
        # 压缩后上下文应包含摘要（作为 system 消息）+ 保留的近期消息
        assert any("摘要" in str(m.get("content", "")) for m in ctx)

    def test_clear(self) -> None:
        """清空后消息列表应为空。"""
        mem = ShortTermMemory(max_messages=20, summary_threshold=15)
        mem.add_message("user", "测试")
        mem.clear()
        assert len(mem.get_context()) == 0
