"""规则模块共享工具：安全数值/日期解析、异常 ID 生成器。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    """把任意值安全地转成 float；失败返回 default。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_positive_float(value: Any) -> float | None:
    """转 float 且要求 > 0；不满足返回 None。"""
    v = safe_float(value, 0.0)
    return v if v > 0 else None


def safe_date(value: Any) -> date | None:
    """安全解析 ISO 日期字符串；非法或为空返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


class AnomalyIdGenerator:
    """线程内顺序生成异常 ID：``ANO-YYYYMMDD-NNNN``。"""

    __slots__ = ("_seq", "_width")

    def __init__(self, width: int = 4) -> None:
        self._seq = 0
        self._width = width

    def reset(self) -> None:
        self._seq = 0

    def next_id(self) -> str:
        self._seq += 1
        today = date.today().strftime("%Y%m%d")
        return f"ANO-{today}-{self._seq:0{self._width}d}"
