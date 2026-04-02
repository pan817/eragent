"""
OWL 本体推理模块。

基于 Owlready2 提供本体推理能力，包括：
- OWL2 推理（类层次、属性传递、等价类）
- SWRL 规则执行（三路匹配、付款合规等核心合规规则）
- 规则触发结果解析

注意：SWRL 推理依赖 HermiT 推理器（需要 Java），
若 Java 不可用则回退到基于 Python 的规则执行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from core.ontology.loader import OntologyLoader, OWLREADY2_AVAILABLE

logger = structlog.get_logger(__name__)


@dataclass
class RuleViolation:
    """SWRL 规则违反记录。"""

    rule_id: str
    """规则唯一标识符"""
    rule_name: str
    """规则名称（中文）"""
    subject_id: str
    """违反规则的业务单据 ID"""
    subject_type: str
    """业务单据类型"""
    details: dict[str, Any] = field(default_factory=dict)
    """违反细节，包含具体数值"""
    detected_at: datetime = field(default_factory=datetime.utcnow)
    """检测时间"""


# P2P 业务规则定义（SWRL 规则的 Python 等价实现）
# 格式：规则ID -> 规则元数据
P2P_RULES: dict[str, dict[str, str]] = {
    "RULE_P2P_THREE_WAY_MATCH_AMOUNT": {
        "name": "三路匹配金额偏差规则",
        "description": "发票金额与采购订单金额偏差超过容差时标记异常",
        "category": "three_way_match",
        "swrl": (
            "PurchaseOrder(?po) ^ Invoice(?inv) ^ referencedPO(?inv, ?po) ^ "
            "amount(?po, ?po_amt) ^ amount(?inv, ?inv_amt) ^ "
            "swrlb:divide(?ratio, ?inv_amt, ?po_amt) ^ "
            "swrlb:subtract(?var, ?ratio, 1.0) ^ "
            "swrlb:abs(?abs_var, ?var) ^ "
            "swrlb:greaterThan(?abs_var, 0.05) "
            "-> ThreeWayMatchAnomaly(?violation)"
        ),
    },
    "RULE_P2P_THREE_WAY_MATCH_QUANTITY": {
        "name": "三路匹配数量偏差规则",
        "description": "发票数量与收货数量偏差超过容差时标记异常",
        "category": "three_way_match",
        "swrl": (
            "ReceiptTransaction(?rcv) ^ Invoice(?inv) ^ "
            "quantity(?rcv, ?rcv_qty) ^ quantity(?inv, ?inv_qty) ^ "
            "swrlb:greaterThan(?rcv_qty, 0) ^ "
            "swrlb:divide(?ratio, ?inv_qty, ?rcv_qty) ^ "
            "swrlb:subtract(?var, ?ratio, 1.0) ^ "
            "swrlb:abs(?abs_var, ?var) ^ "
            "swrlb:greaterThan(?abs_var, 0.05) "
            "-> ThreeWayMatchAnomaly(?violation)"
        ),
    },
    "RULE_P2P_PAYMENT_OVERDUE": {
        "name": "付款逾期规则",
        "description": "实际付款日超过付款条款到期日",
        "category": "payment_compliance",
        "swrl": (
            "Payment(?pmt) ^ Invoice(?inv) ^ appliedToInvoice(?pmt, ?inv) ^ "
            "dueDate(?inv, ?due) ^ paymentDate(?pmt, ?paid) ^ "
            "swrlb:greaterThan(?paid, ?due) "
            "-> PaymentComplianceAnomaly(?violation)"
        ),
    },
    "RULE_P2P_PAYMENT_EARLY": {
        "name": "提前付款预警规则",
        "description": "实际付款日早于付款条款到期日超过配置天数",
        "category": "payment_compliance",
        "swrl": (
            "Payment(?pmt) ^ Invoice(?inv) ^ appliedToInvoice(?pmt, ?inv) ^ "
            "dueDate(?inv, ?due) ^ paymentDate(?pmt, ?paid) ^ "
            "swrlb:lessThan(?paid, ?due) "
            "-> PaymentComplianceAnomaly(?violation)"
        ),
    },
    "RULE_P2P_DISCOUNT_ABUSE": {
        "name": "折扣滥用规则",
        "description": "超过折扣有效期仍按折扣金额付款",
        "category": "payment_compliance",
        "swrl": (
            "Payment(?pmt) ^ Invoice(?inv) ^ appliedToInvoice(?pmt, ?inv) ^ "
            "paymentDate(?pmt, ?paid) ^ dueDate(?inv, ?discount_due) ^ "
            "swrlb:greaterThan(?paid, ?discount_due) ^ "
            "swrlb:lessThan(?amount(?pmt), ?amount(?inv)) "
            "-> PaymentComplianceAnomaly(?violation)"
        ),
    },
}


class OntologyReasoner:
    """
    OWL 本体推理器。

    封装 Owlready2 推理能力，提供：
    1. 本体一致性验证
    2. SWRL 规则元数据查询（规则文档）
    3. 规则 ID 到规则描述的映射（供异常记录引用）
    """

    def __init__(self, loader: OntologyLoader) -> None:
        """
        初始化推理器。

        Args:
            loader: 已加载本体的 OntologyLoader 实例。
        """
        self._loader = loader
        self._reasoner_available = False
        self._sync_reasoner: Any = None
        self._init_reasoner()

    def _init_reasoner(self) -> None:
        """初始化 HermiT/Pellet 推理器（如果可用）。"""
        if not OWLREADY2_AVAILABLE:
            logger.warning("owlready2 不可用，推理功能降级为 Python 规则执行")
            return

        try:
            from owlready2 import sync_reasoner_pellet, sync_reasoner_hermit  # type: ignore
            self._sync_reasoner = sync_reasoner_pellet
            self._reasoner_available = True
            logger.info("推理器初始化成功", reasoner="Pellet")
        except Exception:
            try:
                from owlready2 import sync_reasoner_hermit  # type: ignore
                self._sync_reasoner = sync_reasoner_hermit
                self._reasoner_available = True
                logger.info("推理器初始化成功", reasoner="HermiT")
            except Exception as e:
                logger.warning(
                    "HermiT/Pellet 推理器不可用（可能缺少 Java）",
                    error=str(e),
                    fallback="Python 规则执行",
                )

    def run_reasoning(self) -> bool:
        """
        执行 OWL 推理（类层次、传递属性等）。

        Returns:
            True 表示推理成功，False 表示推理器不可用（降级模式）。
        """
        if not self._reasoner_available or self._sync_reasoner is None:
            logger.info("推理器不可用，跳过 OWL 推理")
            return False

        try:
            with self._loader.world:
                self._sync_reasoner(
                    self._loader.world,
                    infer_property_values=True,
                    infer_data_property_values=True,
                )
            logger.info("OWL 推理完成")
            return True
        except Exception as e:
            logger.error("OWL 推理失败", error=str(e))
            return False

    def get_rule_by_id(self, rule_id: str) -> dict[str, str] | None:
        """
        按规则 ID 获取规则元数据。

        Args:
            rule_id: 规则唯一标识符，如 'RULE_P2P_THREE_WAY_MATCH_AMOUNT'。

        Returns:
            规则元数据字典，不存在时返回 None。
        """
        return P2P_RULES.get(rule_id)

    def get_rules_by_category(self, category: str) -> dict[str, dict[str, str]]:
        """
        按业务分类获取规则列表。

        Args:
            category: 规则分类，如 'three_way_match'、'payment_compliance'。

        Returns:
            符合条件的规则字典。
        """
        return {
            rule_id: rule_meta
            for rule_id, rule_meta in P2P_RULES.items()
            if rule_meta.get("category") == category
        }

    def get_all_rules(self) -> dict[str, dict[str, str]]:
        """
        获取所有 P2P 业务规则定义。

        Returns:
            所有规则的元数据字典。
        """
        return dict(P2P_RULES)

    def get_rules_context_for_rag(self) -> str:
        """
        生成供 RAG 注入的规则上下文（自然语言描述）。

        Returns:
            格式化的规则描述文本，用于注入 Agent system prompt。
        """
        lines = ["## P2P 核心业务合规规则\n"]
        for rule_id, meta in P2P_RULES.items():
            lines.append(f"### {meta['name']} ({rule_id})")
            lines.append(f"- **描述**: {meta['description']}")
            lines.append(f"- **分类**: {meta['category']}")
            lines.append("")
        return "\n".join(lines)

    def get_ontology_context_for_agent(self) -> dict[str, Any]:
        """
        生成供 P2P Agent 注入的混合上下文（结构化 + 自然语言）。

        返回格式：
        - structured: 关键规则的 JSON 结构（用于精确判断）
        - narrative: 业务背景的自然语言描述（用于 LLM 理解）

        Returns:
            包含 structured 和 narrative 两个键的字典。
        """
        structured = {
            "domain": "P2P (Procure-to-Pay)",
            "core_entities": [
                "PurchaseOrder（采购订单，对应 PO_HEADERS_ALL）",
                "PurchaseOrderLine（采购订单行，对应 PO_LINES_ALL）",
                "ReceiptTransaction（收货事务，对应 RCV_TRANSACTIONS）",
                "Invoice（应付发票，对应 AP_INVOICES_ALL）",
                "Payment（付款记录，对应 AP_PAYMENTS_ALL）",
                "Supplier（供应商，对应 AP_SUPPLIERS）",
            ],
            "compliance_rules": {
                rule_id: {
                    "name": meta["name"],
                    "description": meta["description"],
                }
                for rule_id, meta in P2P_RULES.items()
            },
        }

        narrative = (
            "采购到付款（P2P）流程是企业采购管理的核心流程，"
            "从采购申请开始，经过采购订单审批、供应商发货、收货验收、"
            "发票核销，到最终付款结算。三路匹配是 P2P 合规控制的核心机制，"
            "要求采购订单（PO）、收货单（GR）、供应商发票（Invoice）"
            "在数量和金额上保持一致，偏差超过配置容差时需人工审核。"
            "付款合规性关注付款是否按照合同约定的付款条款执行，"
            "包括逾期付款风险和不合理提前付款导致的现金流损失。"
        )

        return {"structured": structured, "narrative": narrative}
