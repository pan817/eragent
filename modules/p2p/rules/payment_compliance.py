"""
付款合规性检查模块。

检测付款数据中的合规性问题：
1. 逾期付款 (HIGH) — 付款日超过发票到期日
2. 提前付款 (MEDIUM) — 付款日比到期日早于阈值天数
3. 折扣滥用 (HIGH) — 超过折扣截止日仍按折扣金额付款
4. 折扣未使用 (LOW) — 在折扣有效期内全额付款，未使用可用折扣
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from api.schemas.analysis import (
    AnomalyDetail,
    AnomalyRecord,
    DocumentRef,
    Severity,
)
from config.settings import P2PSettings


# 规则 ID 常量
_RULE_OVERDUE = "RULE_P2P_PAYMENT_OVERDUE"
_RULE_EARLY = "RULE_P2P_PAYMENT_EARLY"
_RULE_DISCOUNT_ABUSE = "RULE_P2P_DISCOUNT_ABUSE"


class PaymentComplianceChecker:
    """付款合规性检查器。

    将付款数据与发票数据按 invoice_number 关联，
    逐条检测逾期付款、提前付款和折扣滥用。
    """

    def __init__(self, settings: P2PSettings) -> None:
        """初始化付款合规性检查器。

        Args:
            settings: P2P 模块配置对象，包含付款合规参数。
        """
        self._settings: P2PSettings = settings
        self._seq: int = 0

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def check(
        self,
        payments: list[dict[str, Any]],
        invoices: list[dict[str, Any]],
    ) -> list[AnomalyRecord]:
        """执行付款合规性检查。

        Args:
            payments: 付款记录列表，每条需包含:
                - payment_number (str): 付款单号
                - invoice_number (str): 关联发票号
                - payment_date (str): 付款日期，ISO 格式 (YYYY-MM-DD)
                - payment_amount (float): 实际付款金额
            invoices: 发票记录列表，每条需包含:
                - invoice_number (str): 发票号
                - po_number (str): 关联采购订单号
                - due_date (str): 付款截止日，ISO 格式
                - discount_due_date (str, 可选): 折扣截止日，ISO 格式
                - invoice_amount (float): 发票金额
                - supplier_name (str): 供应商名称

        Returns:
            检测到的合规性异常记录列表。
        """
        self._seq = 0
        anomalies: list[AnomalyRecord] = []

        # 按 invoice_number 索引发票
        inv_map: dict[str, dict[str, Any]] = {
            inv["invoice_number"]: inv for inv in invoices
        }

        compliance_cfg = self._settings.payment_compliance
        early_threshold_days: int = compliance_cfg.early_payment_threshold_days

        for payment in payments:
            invoice_number: str = payment["invoice_number"]
            invoice: dict[str, Any] | None = inv_map.get(invoice_number)
            if invoice is None:
                continue  # 找不到对应发票，跳过

            payment_date: date = date.fromisoformat(payment["payment_date"])
            due_date: date = date.fromisoformat(invoice["due_date"])
            payment_amount: float = float(payment["payment_amount"])
            invoice_amount: float = float(invoice["invoice_amount"])
            payment_number: str = payment.get("payment_number", "")
            po_number: str = invoice.get("po_number", "")
            supplier_name: str = invoice.get("supplier_name", "")

            docs = DocumentRef(
                po_number=po_number,
                invoice_number=invoice_number,
                payment_number=payment_number,
                supplier_name=supplier_name,
            )

            # --- 1. 逾期付款检测 ---
            if payment_date > due_date:
                overdue_days: int = (payment_date - due_date).days
                severity = self._overdue_severity(overdue_days)
                anomalies.append(
                    AnomalyRecord(
                        anomaly_id=self._next_anomaly_id(),
                        anomaly_type="payment_overdue",
                        severity=severity,
                        rule_id=_RULE_OVERDUE,
                        documents=docs,
                        details=AnomalyDetail(
                            field="payment_date",
                            expected_value=None,
                            actual_value=None,
                            variance_pct=None,
                            tolerance_pct=None,
                        ),
                        description=(
                            f"付款单 {payment_number} 逾期 {overdue_days} 天，"
                            f"付款日 {payment_date.isoformat()}，"
                            f"到期日 {due_date.isoformat()}"
                        ),
                        recommended_action=(
                            "请调查逾期原因并采取纠正措施，"
                            "评估是否需要支付滞纳金，优化付款审批流程"
                        ),
                        detected_at=datetime.utcnow(),
                        ontology_evidence=f"规则 {_RULE_OVERDUE} 触发：逾期 {overdue_days} 天",
                    )
                )

            # --- 2. 提前付款检测 ---
            if payment_date < due_date:
                early_days: int = (due_date - payment_date).days
                if early_days > early_threshold_days:
                    anomalies.append(
                        AnomalyRecord(
                            anomaly_id=self._next_anomaly_id(),
                            anomaly_type="payment_early",
                            severity=Severity.MEDIUM,
                            rule_id=_RULE_EARLY,
                            documents=docs,
                            details=AnomalyDetail(
                                field="payment_date",
                                expected_value=float(early_threshold_days),
                                actual_value=float(early_days),
                                variance_pct=None,
                                tolerance_pct=None,
                            ),
                            description=(
                                f"付款单 {payment_number} 提前 {early_days} 天付款，"
                                f"超出提前付款阈值 {early_threshold_days} 天，"
                                f"可能影响企业现金流"
                            ),
                            recommended_action=(
                                "请确认提前付款是否因折扣优惠等合理原因，"
                                "否则建议在到期日前合理安排付款以优化资金利用率"
                            ),
                            detected_at=datetime.utcnow(),
                            ontology_evidence=f"规则 {_RULE_EARLY} 触发：提前 {early_days} 天",
                        )
                    )

            # --- 3. 折扣异常检测 ---
            discount_due_str: str | None = invoice.get("discount_due_date")
            if discount_due_str:
                discount_due_date: date = date.fromisoformat(discount_due_str)
                discount_expected: float = float(invoice.get("discount_amount", 0))

                # 3a. 折扣滥用：付款日超过折扣截止日但仍按折扣金额付款
                if payment_date > discount_due_date and payment_amount < invoice_amount:
                    diff_pct: float = (
                        (invoice_amount - payment_amount) / invoice_amount * 100
                    )
                    anomalies.append(
                        AnomalyRecord(
                            anomaly_id=self._next_anomaly_id(),
                            anomaly_type="discount_abuse",
                            severity=Severity.HIGH,
                            rule_id=_RULE_DISCOUNT_ABUSE,
                            documents=docs,
                            details=AnomalyDetail(
                                field="payment_amount",
                                expected_value=invoice_amount,
                                actual_value=payment_amount,
                                variance_pct=round(diff_pct, 2),
                                tolerance_pct=None,
                            ),
                            description=(
                                f"付款单 {payment_number} 在折扣截止日 "
                                f"{discount_due_date.isoformat()} 之后仍按折扣金额付款，"
                                f"实付 {payment_amount:.2f}，应付 {invoice_amount:.2f}"
                            ),
                            recommended_action=(
                                "请核实是否存在未经授权的折扣扣减，"
                                "补齐差额或与供应商重新协商折扣条款"
                            ),
                            detected_at=datetime.utcnow(),
                            ontology_evidence=f"规则 {_RULE_DISCOUNT_ABUSE} 触发：折扣滥用",
                        )
                    )

                # 3b. 折扣未使用：在折扣有效期内全额付款，未使用可用折扣
                elif (
                    payment_date <= discount_due_date
                    and discount_expected > 0
                    and payment_amount >= invoice_amount
                ):
                    missed_saving: float = invoice_amount - discount_expected
                    if missed_saving > 0:
                        anomalies.append(
                            AnomalyRecord(
                                anomaly_id=self._next_anomaly_id(),
                                anomaly_type="discount_unused",
                                severity=Severity.LOW,
                                rule_id=_RULE_DISCOUNT_ABUSE,
                                documents=docs,
                                details=AnomalyDetail(
                                    field="payment_amount",
                                    expected_value=discount_expected,
                                    actual_value=payment_amount,
                                    variance_pct=round(
                                        missed_saving / invoice_amount * 100, 2
                                    ) if invoice_amount > 0 else None,
                                    tolerance_pct=None,
                                ),
                                description=(
                                    f"付款单 {payment_number} 在折扣有效期内"
                                    f"（截至 {discount_due_date.isoformat()}）全额付款 "
                                    f"{payment_amount:.2f}，未使用可用折扣，"
                                    f"错失节省 {missed_saving:.2f}"
                                ),
                                recommended_action=(
                                    "建议优化付款流程，在折扣有效期内优先使用折扣，"
                                    "以降低采购成本"
                                ),
                                detected_at=datetime.utcnow(),
                                ontology_evidence=f"规则 {_RULE_DISCOUNT_ABUSE} 触发：折扣未使用",
                            )
                        )

        return anomalies

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _overdue_severity(self, overdue_days: int) -> Severity:
        """根据逾期天数判定严重等级。

        规则：
        - 逾期 > high_days (默认 30 天): HIGH
        - 逾期 > low_days (默认 7 天): MEDIUM
        - 逾期 <= low_days: LOW

        Args:
            overdue_days: 逾期天数。

        Returns:
            异常严重等级。
        """
        overdue_cfg: dict[str, int] = self._settings.payment_compliance.overdue_severity
        high_days: int = overdue_cfg.get("high_days", 30)
        low_days: int = overdue_cfg.get("low_days", 7)

        if overdue_days > high_days:
            return Severity.HIGH
        if overdue_days > low_days:
            return Severity.MEDIUM
        return Severity.LOW

    def _next_anomaly_id(self) -> str:
        """生成下一个异常 ID。

        格式: ANO-{日期}-{序号}

        Returns:
            唯一异常 ID 字符串。
        """
        self._seq += 1
        today: str = date.today().strftime("%Y%m%d")
        return f"ANO-{today}-{self._seq:03d}"
