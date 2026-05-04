"""记忆模块：长期记忆（PostgreSQL）+ 短期记忆（LangGraph checkpointer）+ chat 索引。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.memory.long_term import (
    LongTermMemory,
    MemoryRepository,
    ReportRepository,
    get_long_term_memory,
    get_memory_repository,
    get_report_repository,
    reset_long_term_memory,
    reset_repositories,
)
from core.memory.short_term import ShortTermMemory

if TYPE_CHECKING:
    from core.memory.chat_indexer import ChatHistoryIndexer

__all__ = [
    "LongTermMemory",
    "ShortTermMemory",
    "MemoryRepository",
    "ReportRepository",
    "get_long_term_memory",
    "get_memory_repository",
    "get_report_repository",
    "reset_long_term_memory",
    "reset_repositories",
    "get_chat_indexer",
    "init_chat_indexer",
    "reset_chat_indexer",
]

_chat_indexer: ChatHistoryIndexer | None = None


def init_chat_indexer(settings: Any) -> ChatHistoryIndexer:
    """初始化全局 ChatHistoryIndexer 单例。由 app startup 调用。"""
    global _chat_indexer  # noqa: PLW0603
    from core.memory.chat_indexer import ChatHistoryIndexer as _Cls

    _chat_indexer = _Cls(settings)
    return _chat_indexer


def get_chat_indexer() -> ChatHistoryIndexer | None:
    """获取全局 ChatHistoryIndexer（未初始化返回 None）。"""
    return _chat_indexer


def reset_chat_indexer() -> None:
    """重置全局 ChatHistoryIndexer（测试用）。"""
    global _chat_indexer  # noqa: PLW0603
    if _chat_indexer is not None:
        _chat_indexer.close()
    _chat_indexer = None
