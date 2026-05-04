"""时间衰减权重计算。

检索结果按 created_at 距今天数施加指数衰减，近期记忆得分更高。
公式：final_score = base_score * exp(-lambda * days_ago)
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from core.time_utils import now_cn

_logger = logging.getLogger(__name__)


def apply_recency_decay(
    items: list[dict[str, Any]],
    lambda_val: float = 0.02,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """对检索结果施加时间衰减并按 recency_score 降序排序。

    Args:
        items: 带 ``created_at`` (datetime) 和可选 ``base_score`` (float) 的记录列表。
        lambda_val: 衰减系数；0.02 时 30 天前得分约 55%。
        enabled: False 时跳过衰减，仅补 recency_score = base_score。

    Returns:
        原列表（就地修改），追加 ``recency_score`` 字段，按该字段降序排列。
    """
    if not items:
        return items

    reference = now_cn()

    for item in items:
        base = float(item.get("base_score", 1.0))
        created = item.get("created_at")
        if not enabled or created is None:
            item["recency_score"] = base
            continue
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except (ValueError, TypeError):
                item["recency_score"] = base
                continue
        days_ago = max((reference - created) / timedelta(days=1), 0.0)
        try:
            decay = math.exp(-lambda_val * days_ago)
        except (OverflowError, ValueError):
            _logger.warning("recency decay math error, falling back to 1.0")
            decay = 1.0
        item["recency_score"] = base * decay

    items.sort(key=lambda x: x.get("recency_score", 0.0), reverse=True)
    return items
