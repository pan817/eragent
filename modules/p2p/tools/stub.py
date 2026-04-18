"""P2P 存根工具（Phase 4 真实实现，4 个）。"""

from __future__ import annotations

from langchain.tools import tool

from modules.p2p.tools._output import _get_tool_logger


@tool
async def query_material_master(material_ids: str = "") -> str:
    """查询物料主数据信息（存根，Phase 4 实现）。

    Args:
        material_ids: 逗号分隔的物料 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("query_material_master is a stub, returning empty result")
    return "[]"


@tool
async def run_vendor_risk_scoring(vendor_ids: str = "") -> str:
    """执行供应商风险评分（存根，Phase 4 实现）。

    Args:
        vendor_ids: 逗号分隔的供应商 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("run_vendor_risk_scoring is a stub, returning empty result")
    return "[]"


@tool
async def check_approval_limits(po_ids: str = "") -> str:
    """检查采购审批限额合规性（存根，Phase 4 实现）。

    Args:
        po_ids: 逗号分隔的采购订单 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("check_approval_limits is a stub, returning empty result")
    return "[]"


@tool
async def check_blacklist(vendor_ids: str = "") -> str:
    """检查供应商黑名单（存根，Phase 4 实现）。

    Args:
        vendor_ids: 逗号分隔的供应商 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("check_blacklist is a stub, returning empty result")
    return "[]"
