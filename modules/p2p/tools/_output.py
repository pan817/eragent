"""工具输出裁剪。"""

from __future__ import annotations

import json
from typing import Any

_tool_logger: Any = None


def _get_tool_logger() -> Any:
    """延迟获取 logger，避免模块级循环导入。"""
    global _tool_logger
    if _tool_logger is None:
        from core.logging_utils import get_logger
        _tool_logger = get_logger(__name__)
    return _tool_logger


def _clip_and_dump(obj: Any) -> str:
    """把 tool 返回的 Python 对象裁剪后序列化为 JSON 字符串。

    两级预算（读取 ``settings.p2p.tool_output``）：

    1. ``max_items``：列表型结构（顶层 list 或 dict 中的 list 字段）最多保留
       前 N 条，尾部追加一条 ``{"_truncated": true, "dropped": M, "reason": ...}``
       摘要记录，向 LLM 显式暴露"还有 M 条同类数据未列出"。
    2. ``max_chars``：序列化后的 JSON 总字符数兜底；超出时继续按顺序丢弃列表
       尾部元素直到达标（若无列表可裁，最后退化到按字符硬切）。

    注意：只在结构层裁剪，保证截断后仍是合法 JSON；LLM 能正确解析并理解"数据
    已被系统预算裁剪"，而不是解析到半截坏 JSON。
    """
    from modules.p2p.settings import get_p2p_settings

    cfg = get_p2p_settings().tool_output
    max_items = cfg.max_items
    max_chars = cfg.max_chars

    def _clip_list(items: list[Any]) -> list[Any]:
        if max_items <= 0 or len(items) <= max_items:
            return items
        dropped = len(items) - max_items
        return [
            *items[:max_items],
            {
                "_truncated": True,
                "dropped": dropped,
                "reason": f"系统预算裁剪：共 {len(items)} 条，仅展示前 {max_items} 条",
            },
        ]

    # ---- 第一轮：条目数裁剪 ----
    if isinstance(obj, list):
        obj = _clip_list(obj)
    elif isinstance(obj, dict):
        obj = {
            k: _clip_list(v) if isinstance(v, list) else v
            for k, v in obj.items()
        }

    text = json.dumps(obj, ensure_ascii=False, default=str)

    # ---- 第二轮：字符总量兜底 ----
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    # 尝试进一步缩减列表
    if isinstance(obj, list):
        while len(text) > max_chars and len(obj) > 1:
            obj.pop(-2 if isinstance(obj[-1], dict) and obj[-1].get("_truncated") else -1)
            text = json.dumps(obj, ensure_ascii=False, default=str)
    elif isinstance(obj, dict):
        for key in list(obj.keys()):
            if not isinstance(obj[key], list):
                continue
            items = obj[key]
            while len(text) > max_chars and len(items) > 1:
                items.pop(-2 if isinstance(items[-1], dict) and items[-1].get("_truncated") else -1)
                text = json.dumps(obj, ensure_ascii=False, default=str)
            if len(text) <= max_chars:
                break

    # 最终硬切兜底
    if len(text) > max_chars:
        text = text[: max_chars - 50] + '..."_budget_truncated":true}'

    return text
