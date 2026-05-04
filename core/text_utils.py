"""通用文本处理工具。"""

from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)


def trim_to_token_budget(text: str, max_tokens: int, label: str) -> str:
    """将文本裁剪到 token 预算内。

    超出预算时按比例截断并发出 WARNING 日志。
    未超出时原样返回。

    Args:
        text: 待裁剪文本。
        max_tokens: token 上限。
        label: 日志标签（如 "短期记忆"），用于区分来源。

    Returns:
        裁剪后的文本。
    """
    if not text or max_tokens <= 0:
        return text

    from core.observability.tracing import estimate_tokens

    current = estimate_tokens(text)
    if current <= max_tokens:
        return text

    ratio = max_tokens / current
    cut_len = int(len(text) * ratio)
    trimmed = text[:cut_len]
    _logger.warning(
        "%s 超出 token 预算，已裁剪: %d → %d tokens (字符 %d → %d)",
        label, current, max_tokens, len(text), cut_len,
    )
    return trimmed
