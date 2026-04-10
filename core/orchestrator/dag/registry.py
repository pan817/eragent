"""
工具注册表。

将 @tool 函数注册为 {name: callable} 字典，支持别名映射。
DAG Executor 通过 tool_name 查找并调用对应函数。
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from core.logging_utils import get_logger

_logger = get_logger(__name__)

# 工具函数类型：async callable，接受 kwargs，返回 str
ToolFn = Callable[..., Coroutine[Any, Any, str]]


class ToolRegistry:
    """工具注册表，管理 DAG 可用工具及别名。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolFn] = {}

    def register(self, name: str, fn: ToolFn) -> None:
        """注册一个工具函数。"""
        self._tools[name] = fn

    def register_alias(self, alias: str, canonical: str) -> None:
        """为已注册的工具添加别名。"""
        if canonical not in self._tools:
            raise KeyError(f"canonical tool '{canonical}' not registered")
        self._tools[alias] = self._tools[canonical]

    def get(self, name: str) -> ToolFn | None:
        """按名称获取工具函数，未找到返回 None。"""
        return self._tools.get(name)

    @property
    def tool_names(self) -> set[str]:
        """所有已注册的工具名（含别名）。"""
        return set(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._tools


def build_default_registry() -> ToolRegistry:
    """构建包含所有 P2P 工具的默认注册表（含 execute.md 别名）。

    调用时才导入 tools 模块，避免模块级循环依赖。
    """
    from modules.p2p.tools import (
        calculate_spend_analysis,
        calculate_supplier_kpis,
        calculate_po_cycle_time,
        check_approval_limits,
        check_blacklist,
        query_invoices,
        query_payments,
        query_purchase_orders,
        query_receipts,
        query_vendor_master,
        query_material_master,
        run_payment_compliance_check,
        run_price_variance_analysis,
        run_three_way_match,
        run_vendor_risk_scoring,
    )

    registry = ToolRegistry()

    # 现有工具（canonical name）
    for name, fn in [
        ("query_purchase_orders", query_purchase_orders),
        ("query_receipts", query_receipts),
        ("query_invoices", query_invoices),
        ("query_payments", query_payments),
        ("run_three_way_match", run_three_way_match),
        ("run_price_variance_analysis", run_price_variance_analysis),
        ("run_payment_compliance_check", run_payment_compliance_check),
        ("calculate_supplier_kpis", calculate_supplier_kpis),
        # Phase 2 新增
        ("query_vendor_master", query_vendor_master),
        ("calculate_spend_analysis", calculate_spend_analysis),
        # 存根
        ("query_material_master", query_material_master),
        ("calculate_po_cycle_time", calculate_po_cycle_time),
        ("run_vendor_risk_scoring", run_vendor_risk_scoring),
        ("check_approval_limits", check_approval_limits),
        ("check_blacklist", check_blacklist),
    ]:
        registry.register(name, fn)

    # execute.md 别名映射
    registry.register_alias("query_goods_receipts", "query_receipts")
    registry.register_alias("query_vendor_invoices", "query_invoices")
    registry.register_alias("calculate_ppv", "run_price_variance_analysis")
    registry.register_alias("get_vendor_scorecard", "calculate_supplier_kpis")
    registry.register_alias("validate_compliance", "run_payment_compliance_check")

    _logger.info("tool registry built: %d tools (incl. aliases)", len(registry.tool_names))
    return registry
