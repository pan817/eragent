"""会话历史模块：提供 ChatRepository 和表定义。"""

from __future__ import annotations

from core.chat.repository import ChatRepository

_chat_repo: ChatRepository | None = None


def init_chat_repository(repo: ChatRepository) -> None:
    """初始化全局 ChatRepository 实例（由 main.py lifespan 调用）。"""
    global _chat_repo
    _chat_repo = repo


def get_chat_repository() -> ChatRepository | None:
    """获取全局 ChatRepository 实例，未初始化时返回 None。"""
    return _chat_repo


__all__ = ["ChatRepository", "get_chat_repository", "init_chat_repository"]
