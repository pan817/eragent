"""记忆类型枚举。

5 种封闭分类，不允许运行时自由扩展。扩展类型需修改代码 + 配置。
"""

from __future__ import annotations

from enum import StrEnum


class MemoryType(StrEnum):
    """长期记忆类型（封闭枚举）。

    每种类型有明确的写入来源、TTL、整合规则、检索策略——
    类型不是标签，是行为契约。
    """

    USER_PREFERENCE = "user_preference"
    """用户分析偏好（输出格式、关注阈值、常用查询参数）。TTL: 不过期。"""

    ENTITY_PROFILE = "entity_profile"
    """业务实体累积画像（供应商/PO 的历史表现、关键指标）。TTL: 90 天。"""

    ANALYSIS_INSIGHT = "analysis_insight"
    """跨次分析趋势/模式发现。TTL: 60 天。"""

    CORRECTION = "correction"
    """用户对分析结果的修正/纠正。TTL: 180 天。"""

    DOMAIN_FACT = "domain_fact"
    """业务领域事实知识（企业规则、阈值、例外情况）。TTL: 不过期。"""
