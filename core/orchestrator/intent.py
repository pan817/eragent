"""
意图解析模块。

基于关键词匹配的意图识别，将自然语言查询映射到对应的分析类型，
并从查询文本中提取结构化参数（supplier_id、po_number、days 等）。

MVP 阶段不依赖 LLM，使用纯规则匹配实现。
"""

from __future__ import annotations

import re
from typing import Any

from api.schemas.analysis import AnalysisType


class IntentParser:
    """基于关键词的意图解析器。

    将用户的自然语言查询映射为 AnalysisType，
    并从查询中提取可用的业务参数。
    """

    def __init__(self) -> None:
        """初始化关键词到分析类型的映射表。"""
        self._keyword_map: dict[AnalysisType, list[str]] = {
            AnalysisType.THREE_WAY_MATCH: [
                "三路匹配", "匹配", "three way", "three-way", "3way",
            ],
            AnalysisType.PRICE_VARIANCE: [
                "价格差异", "价格", "price", "variance",
            ],
            AnalysisType.PAYMENT_COMPLIANCE: [
                "付款", "逾期", "payment", "overdue",
            ],
            AnalysisType.SUPPLIER_PERFORMANCE: [
                "供应商", "绩效", "kpi", "supplier", "performance",
            ],
        }

    def parse(self, query: str) -> tuple[AnalysisType, dict[str, Any]]:
        """解析自然语言查询，返回分析类型和提取的参数。

        Args:
            query: 用户输入的自然语言查询文本。

        Returns:
            二元组 (analysis_type, params)：
            - analysis_type: 识别出的分析类型枚举值。
            - params: 从查询中提取的结构化参数字典，
              可能包含 supplier_id、po_number、days 等键。
        """
        analysis_type = self._match_type(query)
        params = self._extract_params(query)
        return analysis_type, params

    def _match_type(self, query: str) -> AnalysisType:
        """根据关键词匹配确定分析类型。

        如果命中多个不同类型的关键词，则返回 COMPREHENSIVE；
        如果无命中，也返回 COMPREHENSIVE。

        Args:
            query: 用户查询文本。

        Returns:
            匹配到的分析类型。
        """
        query_lower = query.lower()
        matched_types: set[AnalysisType] = set()

        for analysis_type, keywords in self._keyword_map.items():
            for keyword in keywords:
                if keyword.lower() in query_lower:
                    matched_types.add(analysis_type)
                    break

        if len(matched_types) == 1:
            return matched_types.pop()

        # 多个命中或无命中均返回综合分析
        return AnalysisType.COMPREHENSIVE

    def _extract_params(self, query: str) -> dict[str, Any]:
        """从查询文本中用正则提取业务参数。

        支持提取的参数：
        - supplier_id: 供应商编号，如 SUP-001、S12345
        - po_number: 采购订单号，如 PO-20250101-001、PO12345
        - days: 天数，如 "最近30天"、"past 90 days"

        Args:
            query: 用户查询文本。

        Returns:
            提取到的参数字典，仅包含实际匹配到的键。
        """
        params: dict[str, Any] = {}

        # 提取供应商编号：SUP-xxx 或 S+数字
        supplier_match = re.search(
            r"(?:SUP|sup|S)-?\d+[-\w]*", query
        )
        if supplier_match:
            params["supplier_id"] = supplier_match.group()

        # 提取采购订单号：PO-xxx 或 PO+数字
        po_match = re.search(
            r"(?:PO|po)-?[\d][\d\w-]*", query
        )
        if po_match:
            params["po_number"] = po_match.group()

        # 提取天数：中文 "最近N天" 或英文 "past/last N days"
        days_match = re.search(
            r"(?:最近|过去|近)\s*(\d+)\s*天", query
        )
        if not days_match:
            days_match = re.search(
                r"(?:past|last|recent)\s+(\d+)\s*days?", query, re.IGNORECASE
            )
        if days_match:
            params["days"] = int(days_match.group(1))

        return params
