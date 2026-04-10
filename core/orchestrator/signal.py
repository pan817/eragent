"""
查询信号数据结构。

QuerySignal 是意图路由的中间表示，封装从用户请求中提取的结构化信息。
Level 1/2 由正则 + 规则填充，Level 3 由 LLM 填充。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QuerySignal:
    """从用户请求中提取的结构化信号。

    Attributes:
        raw_query: 用户原始查询文本。
        keywords: 提取的业务关键词列表。
        entities: 识别的业务实体（supplier_id / po_number 等）。
        time_range_days: 提取的时间范围（天），None 表示未识别。
        route_level: 命中的路由层级（1 / 2 / 3）。
        confidence: 路由置信度（0.0~1.0）。
        reasoning: 路由决策的可读说明，用于 trace 和调试。
    """

    raw_query: str
    keywords: list[str] = field(default_factory=list)
    entities: dict[str, Any] = field(default_factory=dict)
    time_range_days: int | None = None
    route_level: int = 0
    confidence: float = 0.0
    reasoning: str = ""
