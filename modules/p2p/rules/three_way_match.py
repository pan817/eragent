"""
三路匹配异常检测模块。

比对采购订单（PO）、收货单（GR）、供应商发票（Invoice）的金额和数量，
当偏差超过配置容差时生成异常记录。容差支持按供应商、物料类别、金额区间配置。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from api.schemas.analysis import (
    AnomalyDetail,
    AnomalyRecord,
    DocumentRef,
    Severity,
)
from config.settings import P2PSettings

# 规则 ID 常量（对应 core.ontology.reasoner.P2P_RULES）
_RULE_AMOUNT = "RULE_P2P_THREE_WAY_MATCH_AMOUNT"
_RULE_QUANTITY = "RULE_P2P_THREE_WAY_MATCH_QUANTITY"


class ThreeWayMatchChecker:
    """三路匹配异常检测器。

    根据 PO、GR、Invoice 三方数据进行金额和数量匹配检测，
    超出容差范围的记录生成 AnomalyRecord。

    容差优先级（从高到低）：
        1. 供应商级别容差 (supplier_tolerances)
        2. 物料类别容差 (category_tolerances)
        3. 金额区间容差 (amount_thresholds)
        4. 全局默认容差 (default_tolerance_pct)

    严重等级判定规则：
        - 超容差 2 倍以上 或 金额 > 50 万 -> HIGH
        - 超容差 1-2 倍 -> MEDIUM
        - 接近边界 (90%-100% 容差) -> LOW
    """

    def __init__(self, settings: P2PSettings) -> None:
        """初始化三路匹配检测器。

        Args:
            settings: P2P 模块配置，包含容差和严重等级参数。
        """
        self._settings: P2PSettings = settings
        self._seq: int = 0

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def check(
        self,
        po_lines: list[dict[str, Any]],
        gr_lines: list[dict[str, Any]],
        invoice_lines: list[dict[str, Any]],
    ) -> list[AnomalyRecord]:
        """执行三路匹配检测。

        按 po_number 关联三方数据，比对金额偏差和数量偏差，
        超出容差则生成 AnomalyRecord。

        Args:
            po_lines: 采购订单行列表，每条需包含:
                - po_number (str): 采购订单号
                - po_amount (float): 采购金额
                - po_quantity (float): 采购数量
                - supplier_id (str, 可选): 供应商 ID
                - supplier_name (str, 可选): 供应商名称
                - material_category (str, 可选): 物料类别
            gr_lines: 收货行列表，每条需包含:
                - po_number (str): 关联采购订单号
                - gr_number (str): 收货单号
                - gr_quantity (float): 收货数量
            invoice_lines: 发票行列表，每条需包含:
                - po_number (str): 关联采购订单号
                - invoice_number (str): 发票号
                - invoice_amount (float): 发票金额

        Returns:
            检测到的异常记录列表。
        """
        self._seq = 0
        anomalies: list[AnomalyRecord] = []

        # 按 po_number 索引 GR 和 Invoice 数据
        gr_by_po: dict[str, list[dict[str, Any]]] = defaultdict(list)
        inv_by_po: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for gr in gr_lines:
            gr_by_po[gr["po_number"]].append(gr)

        for inv in invoice_lines:
            inv_by_po[inv["po_number"]].append(inv)

        for po in po_lines:
            po_number: str = po["po_number"]
            po_amount: float = float(po.get("po_amount", 0.0))
            po_quantity: float = float(po.get("po_quantity", 0.0))
            supplier_id: str = po.get("supplier_id", "")
            supplier_name: str = po.get("supplier_name", "")
            material_category: str = po.get("material_category", "")

            tolerance: float = self._resolve_tolerance(
                supplier_id=supplier_id,
                category=material_category,
                amount=po_amount,
            )

            # --- 金额偏差检测: PO vs Invoice ---
            for inv in inv_by_po.get(po_number, []):
                invoice_amount: float = float(inv.get("invoice_amount", 0.0))
                if po_amount == 0:
                    continue

                variance_pct = abs(invoice_amount - po_amount) / po_amount * 100
                if variance_pct > tolerance:
                    severity = self._determine_severity(variance_pct, tolerance, invoice_amount)
                    anomalies.append(
                        self._build_record(
                            rule_id=_RULE_AMOUNT,
                            anomaly_type="three_way_match_amount",
                            severity=severity,
                            documents=DocumentRef(
                                po_number=po_number,
                                invoice_number=inv.get("invoice_number", ""),
                                supplier_name=supplier_name,
                            ),
                            detail=AnomalyDetail(
                                field="invoice_amount",
                                expected_value=po_amount,
                                actual_value=invoice_amount,
                                variance_pct=round(variance_pct, 2),
                                tolerance_pct=tolerance,
                            ),
                            description=(
                                f"发票金额与采购订单金额偏差 {variance_pct:.2f}%，"
                                f"超出容差 {tolerance:.1f}%"
                            ),
                            recommended_action="请核实发票金额是否正确，必要时联系供应商更正",
                        )
                    )
                elif variance_pct >= tolerance * 0.9:
                    # 接近边界 (90%-100% 容差)，标记 LOW 预警
                    anomalies.append(
                        self._build_record(
                            rule_id=_RULE_AMOUNT,
                            anomaly_type="three_way_match_amount",
                            severity=Severity.LOW,
                            documents=DocumentRef(
                                po_number=po_number,
                                invoice_number=inv.get("invoice_number", ""),
                                supplier_name=supplier_name,
                            ),
                            detail=AnomalyDetail(
                                field="invoice_amount",
                                expected_value=po_amount,
                                actual_value=invoice_amount,
                                variance_pct=round(variance_pct, 2),
                                tolerance_pct=tolerance,
                            ),
                            description=(
                                f"发票金额与采购订单金额偏差 {variance_pct:.2f}%，"
                                f"接近容差上限 {tolerance:.1f}%（预警）"
                            ),
                            recommended_action="偏差接近容差边界，建议关注后续发票",
                        )
                    )

            # --- 数量偏差检测: PO vs GR ---
            for gr in gr_by_po.get(po_number, []):
                gr_quantity: float = float(gr.get("gr_quantity", 0.0))
                if po_quantity == 0:
                    continue

                qty_variance_pct = abs(gr_quantity - po_quantity) / po_quantity * 100
                if qty_variance_pct > tolerance:
                    severity = self._determine_severity(qty_variance_pct, tolerance, po_amount)
                    anomalies.append(
                        self._build_record(
                            rule_id=_RULE_QUANTITY,
                            anomaly_type="three_way_match_quantity",
                            severity=severity,
                            documents=DocumentRef(
                                po_number=po_number,
                                gr_number=gr.get("gr_number", ""),
                                supplier_name=supplier_name,
                            ),
                            detail=AnomalyDetail(
                                field="gr_quantity",
                                expected_value=po_quantity,
                                actual_value=gr_quantity,
                                variance_pct=round(qty_variance_pct, 2),
                                tolerance_pct=tolerance,
                            ),
                            description=(
                                f"收货数量与采购订单数量偏差 {qty_variance_pct:.2f}%，"
                                f"超出容差 {tolerance:.1f}%"
                            ),
                            recommended_action="请核实收货数量，必要时补发或退货处理",
                        )
                    )
                elif qty_variance_pct >= tolerance * 0.9:
                    anomalies.append(
                        self._build_record(
                            rule_id=_RULE_QUANTITY,
                            anomaly_type="three_way_match_quantity",
                            severity=Severity.LOW,
                            documents=DocumentRef(
                                po_number=po_number,
                                gr_number=gr.get("gr_number", ""),
                                supplier_name=supplier_name,
                            ),
                            detail=AnomalyDetail(
                                field="gr_quantity",
                                expected_value=po_quantity,
                                actual_value=gr_quantity,
                                variance_pct=round(qty_variance_pct, 2),
                                tolerance_pct=tolerance,
                            ),
                            description=(
                                f"收货数量与采购订单数量偏差 {qty_variance_pct:.2f}%，"
                                f"接近容差上限 {tolerance:.1f}%（预警）"
                            ),
                            recommended_action="偏差接近容差边界，建议持续监控",
                        )
                    )

        return anomalies

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _resolve_tolerance(
        self,
        supplier_id: str,
        category: str,
        amount: float,
    ) -> float:
        """按优先级获取适用容差百分比。

        优先级（从高到低）：供应商 > 物料类别 > 金额区间 > 默认值。

        Args:
            supplier_id: 供应商 ID。
            category: 物料类别。
            amount: 采购金额，用于匹配金额区间容差。

        Returns:
            适用的容差百分比。
        """
        cfg = self._settings.three_way_match

        # 1. 供应商级别容差
        if supplier_id and supplier_id in cfg.supplier_tolerances:
            return cfg.supplier_tolerances[supplier_id]

        # 2. 物料类别容差
        if category and category in cfg.category_tolerances:
            return cfg.category_tolerances[category]

        # 3. 金额区间容差
        for threshold in cfg.amount_thresholds:
            min_amt: float = float(threshold.get("min_amount", 0.0))
            max_amt: float = float(threshold.get("max_amount", float("inf")))
            if min_amt <= amount < max_amt:
                return float(threshold.get("tolerance_pct", cfg.default_tolerance_pct))

        # 4. 默认容差
        return cfg.default_tolerance_pct

    def _determine_severity(
        self,
        variance_pct: float,
        tolerance: float,
        amount: float,
    ) -> Severity:
        """根据偏差程度和金额确定异常严重等级。

        Args:
            variance_pct: 偏差百分比。
            tolerance: 适用容差百分比。
            amount: 涉及金额。

        Returns:
            异常严重等级。
        """
        severity_cfg = self._settings.anomaly_severity
        high_multiplier: float = severity_cfg.variance_high_multiplier
        high_amount: float = severity_cfg.high_amount_threshold

        ratio: float = variance_pct / tolerance if tolerance > 0 else float("inf")

        if ratio >= high_multiplier or amount > high_amount:
            return Severity.HIGH
        if ratio >= 1.0:
            return Severity.MEDIUM
        return Severity.LOW

    def _next_anomaly_id(self) -> str:
        """生成下一个异常 ID，格式: ANO-{YYYYMMDD}-{序号}。"""
        self._seq += 1
        today_str: str = date.today().strftime("%Y%m%d")
        return f"ANO-{today_str}-{self._seq:04d}"

    def _build_record(
        self,
        *,
        rule_id: str,
        anomaly_type: str,
        severity: Severity,
        documents: DocumentRef,
        detail: AnomalyDetail,
        description: str,
        recommended_action: str,
    ) -> AnomalyRecord:
        """构建异常记录对象。

        Args:
            rule_id: 触发的规则 ID。
            anomaly_type: 异常类型编码。
            severity: 严重等级。
            documents: 关联的业务单据引用。
            detail: 异常明细。
            description: 异常描述（中文）。
            recommended_action: 建议处理动作（中文）。

        Returns:
            构建完成的 AnomalyRecord 实例。
        """
        return AnomalyRecord(
            anomaly_id=self._next_anomaly_id(),
            anomaly_type=anomaly_type,
            severity=severity,
            documents=documents,
            rule_id=rule_id,
            details=detail,
            description=description,
            recommended_action=recommended_action,
            detected_at=datetime.utcnow(),
            ontology_evidence=f"规则 {rule_id} 触发",
        )
