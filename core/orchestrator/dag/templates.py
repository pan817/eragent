"""DAG 模板加载入口。

模板定义已迁入各业务模块（如 modules/p2p/dag_templates.py），
本文件仅保留通用加载逻辑和参数替换。
"""

from __future__ import annotations

import copy
from typing import Any

from api.schemas.domain import AnalysisType


def _replace_params(tasks: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    """将 DAG 模板中的参数占位符替换为实际值。

    支持两种占位符：
    - 纯占位符："{days}" → 直接替换为 params["days"]
    - 嵌入占位符："近{days}天" → 字符串内插替换
    """
    result = copy.deepcopy(tasks)
    for task in result:
        inputs = task.get("inputs", {})
        for key, val in list(inputs.items()):
            if not isinstance(val, str):
                continue
            # 纯占位符
            stripped = val.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                param_name = stripped[1:-1]
                if param_name in params:
                    inputs[key] = params[param_name]
            else:
                # 嵌入占位符
                for pname, pval in params.items():
                    placeholder = "{" + pname + "}"
                    if placeholder in val:
                        inputs[key] = val.replace(placeholder, str(pval))
                        val = inputs[key]
    return result


def load_dag_template(
    analysis_type: AnalysisType,
    params: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """加载并参数化 DAG 模板。

    从 P2P 模块获取模板映射。

    选择逻辑（优先级从高到低）：
    1. COMPREHENSIVE + payment_number → 单笔付款单合规 DAG
    2. COMPREHENSIVE + invoice_number → 单笔发票分析 DAG
    3. COMPREHENSIVE + po_number → PO 综合风险 DAG
    4. COMPREHENSIVE + supplier_id → 供应商综合分析 DAG
    5. 按 analysis_type 匹配分析类型维度模板
    6. 无匹配 → 返回 None（由调用方降级到 agent）
    """
    from modules.p2p.dag_templates import get_entity_template_map, get_template_map

    template_map = get_template_map()
    entity_template_map = get_entity_template_map()

    effective_params = {
        "days": params.get("days", 30),
        "supplier_id": params.get("supplier_id", ""),
        "po_number": params.get("po_number", ""),
        "invoice_number": params.get("invoice_number", ""),
        "payment_number": params.get("payment_number", ""),
        "receipt_number": params.get("receipt_number", ""),
    }

    # 实体维度模板优先：有具体实体 + COMPREHENSIVE 意图
    if analysis_type == AnalysisType.COMPREHENSIVE:
        if effective_params["payment_number"]:
            template = entity_template_map.get("payment_single")
            if template:
                return _replace_params(template, effective_params)
        if effective_params["invoice_number"]:
            template = entity_template_map.get("invoice_single")
            if template:
                return _replace_params(template, effective_params)
        if effective_params["po_number"]:
            template = entity_template_map.get("po_risk")
            if template:
                return _replace_params(template, effective_params)
        if effective_params["supplier_id"]:
            template = entity_template_map.get("supplier_risk")
            if template:
                return _replace_params(template, effective_params)

    # 分析类型维度模板
    template = template_map.get(analysis_type)
    if template is None:
        return None

    return _replace_params(template, effective_params)
