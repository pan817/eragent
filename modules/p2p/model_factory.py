"""向后兼容：model_factory 已迁移到 core.llm.model_factory。"""

from core.llm.model_factory import build_chat_model  # noqa: F401

__all__ = ["build_chat_model"]
