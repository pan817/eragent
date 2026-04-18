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


# execute.md 中使用的工具别名（canonical_name → alias_name 列表）
_TOOL_ALIASES: list[tuple[str, str]] = [
    ("query_goods_receipts", "query_receipts"),
    ("query_vendor_invoices", "query_invoices"),
    ("calculate_ppv", "run_price_variance_analysis"),
    ("get_vendor_scorecard", "calculate_supplier_kpis"),
    ("validate_compliance", "run_payment_compliance_check"),
]


def build_registry_from_provider(provider: Any) -> ToolRegistry:
    """从 ModuleProvider 自动注册全部工具。

    工具名取自 @tool 装饰器生成的 ``.name`` 属性（即函数名）。
    """
    registry = ToolRegistry()
    for tool_fn in provider.get_tools():
        name = getattr(tool_fn, "name", None) or tool_fn.__name__
        registry.register(name, tool_fn)

    for alias, canonical in _TOOL_ALIASES:
        if canonical in registry:
            registry.register_alias(alias, canonical)

    _logger.info("tool registry built: %d tools (incl. aliases)", len(registry.tool_names))
    return registry


def build_default_registry() -> ToolRegistry:
    """兼容入口：无 Provider 时从 P2P 模块直接构建注册表。"""
    from modules.p2p.provider import P2PModuleProvider

    return build_registry_from_provider(P2PModuleProvider())
