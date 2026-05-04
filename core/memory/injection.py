"""记忆注入格式化 — 将分桶检索结果格式化为 LLM prompt 注入文本。

格式设计原则：
- 按类型分区，LLM 能清楚区分不同来源
- correction 和 domain_fact 标注为"必须遵循"
- entity_profile 提供历史上下文
- user_preference 指导输出格式
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

_logger = logging.getLogger(__name__)


def format_memory_injection(
    memories: dict[str, list[dict[str, Any]]],
    type_budgets: dict[str, int] | None = None,
) -> str:
    """将分桶检索结果格式化为 LLM prompt 注入文本。

    Args:
        memories: {memory_type: [记忆列表]}，由 search_by_type 返回
        type_budgets: 各类型的 token 预算百分比（当前版本按字符截断近似）

    Returns:
        格式化后的 prompt 文本，空字符串表示无可用记忆。
    """
    sections: list[str] = []

    # correction — 最高优先级
    _append_section(
        sections,
        memories.get("correction", []),
        "[历史修正记录 — 分析时必须遵循]",
    )

    # domain_fact — 业务规则
    _append_section(
        sections,
        memories.get("domain_fact", []),
        "[业务规则 — 分析时必须遵循]",
    )

    # entity_profile — 实体历史画像
    _append_section(
        sections,
        memories.get("entity_profile", []),
        "[相关实体历史画像]",
    )

    # analysis_insight — 趋势参考（标注时间，提示 LLM 以当前数据为准）
    _append_insight_section(
        sections,
        memories.get("analysis_insight", []),
    )

    # session_recap — 历史会话回顾
    _append_section(
        sections,
        memories.get("session_recap", []),
        "[相关历史会话回顾]",
    )

    # user_preference — 输出偏好
    _append_section(
        sections,
        memories.get("user_preference", []),
        "[用户偏好]",
    )

    if not sections:
        return ""

    return "\n\n".join(sections)


def _append_section(
    sections: list[str],
    items: list[dict[str, Any]],
    header: str,
) -> None:
    """将记忆列表格式化为一个区块并追加到 sections。"""
    if not items:
        return
    lines = [header]
    for m in items:
        content = m.get("content", "")
        if content:
            lines.append(f"- {content}")
    if len(lines) > 1:  # header + at least one item
        sections.append("\n".join(lines))


def _append_insight_section(
    sections: list[str],
    items: list[dict[str, Any]],
) -> None:
    """格式化 analysis_insight，标注时间以提示 LLM 区分历史推测与当前事实。"""
    if not items:
        return
    lines = ["[历史趋势参考 — 仅供参考，以本次查询的实际数据为准]"]
    from core.time_utils import now_cn
    now = now_cn()
    for m in items:
        content = m.get("content", "")
        if not content:
            continue
        created_at = m.get("created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            days_ago = (now - created_at).days
            lines.append(f"- ({days_ago}天前) {content}")
        else:
            lines.append(f"- {content}")
    if len(lines) > 1:
        sections.append("\n".join(lines))
