"""DAG 模板加载入口。

模板定义已迁入各业务模块（如 modules/p2p/dag_templates.py），
本文件仅保留通用加载逻辑和参数替换。
"""

from __future__ import annotations

import copy
import re
from typing import Any

from api.schemas.domain import AnalysisType


def _replace_params(tasks: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    """将 DAG 模板中的参数占位符替换为实际值。

    支持两种占位符：
    - 纯占位符："{days}" → 直接替换为 params["days"]
    - 嵌入占位符："近{days}天" → 字符串内插替换

    未匹配的占位符会被清理为类型安全的默认值（"days" → 30，其余 → ""），
    防止 "{vendor_id}" 等字面量字符串泄漏到工具参数中被当作 truthy 值。
    """
    # 占位符名 → 未匹配时的安全默认值
    _DEFAULTS: dict[str, Any] = {"days": 30}

    result = copy.deepcopy(tasks)
    for task in result:
        inputs = task.get("inputs", {})
        for key, val in list(inputs.items()):
            if not isinstance(val, str):
                continue
            # 纯占位符
            stripped = val.strip()
            if stripped.startswith("{") and stripped.endswith("}") and stripped.count("{") == 1:
                param_name = stripped[1:-1]
                inputs[key] = params.get(param_name, _DEFAULTS.get(param_name, ""))
            else:
                # 嵌入占位符：用正则替换所有 {xxx}
                def _sub(m: re.Match[str], _p: dict[str, Any] = params) -> str:
                    name = m.group(1)
                    return str(_p.get(name, _DEFAULTS.get(name, "")))
                inputs[key] = re.sub(r"\{(\w+)\}", _sub, val)
    return result


def load_dag_template(
    analysis_type: AnalysisType,
    params: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """加载并参数化 DAG 模板。

    从 P2P 模块获取模板映射。

    选择逻辑（优先级从高到低）：
    1. COMPREHENSIVE + check_number → 单笔付款单合规 DAG
    2. COMPREHENSIVE + invoice_num → 单笔发票分析 DAG
    3. COMPREHENSIVE + po_number → PO 综合风险 DAG
    4. COMPREHENSIVE + vendor_id → 供应商综合分析 DAG
    5. 按 analysis_type 匹配分析类型维度模板
    6. 无匹配 → 返回 None（由调用方降级到 agent）
    """
    from modules.p2p.dag_templates import get_entity_template_map, get_template_map

    template_map = get_template_map()
    entity_template_map = get_entity_template_map()

    effective_params = {
        "days": params.get("days", 30),
        "vendor_id": params.get("vendor_id", ""),
        "po_number": params.get("po_number", ""),
        "invoice_num": params.get("invoice_num", ""),
        "check_number": params.get("check_number", ""),
        "receipt_number": params.get("receipt_number", ""),
    }

    # 实体维度模板优先：有具体实体 + COMPREHENSIVE 意图
    if analysis_type == AnalysisType.COMPREHENSIVE:
        if effective_params["check_number"]:
            template = entity_template_map.get("payment_single")
            if template:
                return _replace_params(template, effective_params)
        if effective_params["invoice_num"]:
            template = entity_template_map.get("invoice_single")
            if template:
                return _replace_params(template, effective_params)
        if effective_params["po_number"]:
            template = entity_template_map.get("po_risk")
            if template:
                return _replace_params(template, effective_params)
        if effective_params["vendor_id"]:
            template = entity_template_map.get("supplier_risk")
            if template:
                return _replace_params(template, effective_params)

    # 分析类型维度模板
    template = template_map.get(analysis_type)
    if template is None:
        return None

    return _replace_params(template, effective_params)


def load_generic_template(
    template_key: str,
    params: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """加载并参数化通用概览模板（不按 AnalysisType 索引）。

    Args:
        template_key: 模板键名（如 "recent_procurement_health"）。
        params: 参数字典（至少含 days）。

    Returns:
        参数化后的 DAG 任务列表，或 None（键不存在）。
    """
    from modules.p2p.dag_templates import get_generic_template_map

    generic_map = get_generic_template_map()
    template = generic_map.get(template_key)
    if template is None:
        return None

    effective_params = {
        "days": params.get("days", 30),
        "vendor_id": params.get("vendor_id", ""),
        "po_number": params.get("po_number", ""),
        "invoice_num": params.get("invoice_num", ""),
        "check_number": params.get("check_number", ""),
        "receipt_number": params.get("receipt_number", ""),
    }
    return _replace_params(template, effective_params)
