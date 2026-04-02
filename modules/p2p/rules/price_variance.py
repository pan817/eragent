"""
采购价格差异分析模块。

比对 PO 行项目实际采购单价与合同价/标准价，
偏差超出容差阈值时生成 AnomalyRecord。容差复用三路匹配的 default_tolerance_pct。
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
_RULE_PRICE_VARIANCE = "RULE_P2P_PRICE_VARIANCE"


class PriceVarianceAnalyzer:
    """采购价格差异分析器。

    比较 PO 行项目的实际采购单价与合同价/标准价，
    偏差超容差时生成异常记录。

    严重等级判定规则：
        - 偏差超容差 2 倍以上或金额 > 50 万: HIGH
        - 偏差在容差 1-2 倍之间: MEDIUM
        - 偏差接近边界: LOW
    """

    def __init__(self, settings: P2PSettings) -> None:
        """初始化价格差异分析器。

        Args:
            settings: P2P 模块配置对象，包含容差和严重等级阈值。
        """
        self._settings: P2PSettings = settings
        self._seq: int = 0

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def analyze(
        self,
        po_lines: list[dict[str, Any]],
        contract_prices: dict[str, float],
    ) -> list[AnomalyRecord]:
        """分析 PO 行项目的价格差异。

        将每行 PO 的实际采购单价与 contract_prices 中的合同价/标准价比较，
        计算偏差百分比，超过容差时生成异常记录。

        Args:
            po_lines: PO 行项目列表，每条需包含:
                - po_number (str): 采购订单号
                - line_number (str | int, 可选): 行号
                - material_code (str): 物料编码，用于在 contract_prices 中查找标准价
                - unit_price (float): 实际采购单价
                - quantity (float, 可选): 采购数量
                - supplier_name (str, 可选): 供应商名称
                - material_name (str, 可选): 物料描述
            contract_prices: 合同价/标准价字典，key 为物料编码，value 为标准单价。

        Returns:
            检测到的价格差异异常记录列表。
        """
        self._seq = 0
        anomalies: list[AnomalyRecord] = []

        # 复用三路匹配的默认容差
        tolerance_pct: float = self._settings.three_way_match.default_tolerance_pct

        for line in po_lines:
            material_code: str = line.get("material_code", "")
            unit_price: float = float(line.get("unit_price", 0))

            # 在合同价字典中查找标准价
            standard_price: float | None = contract_prices.get(material_code)
            if standard_price is None or standard_price == 0:
                continue

            variance_pct: float = abs(unit_price - standard_price) / standard_price * 100

            if variance_pct <= tolerance_pct:
                continue

            severity = self._determine_severity(variance_pct, tolerance_pct, unit_price)
            po_number: str = line.get("po_number", "")
            supplier_name: str = line.get("supplier_name", "")
            line_number: str = str(line.get("line_number", ""))
            material_name: str = line.get("material_name", material_code)

            anomalies.append(
                AnomalyRecord(
                    anomaly_id=self._next_anomaly_id(),
                    anomaly_type="price_variance",
                    severity=severity,
                    rule_id=_RULE_PRICE_VARIANCE,
                    documents=DocumentRef(
                        po_number=po_number,
                        supplier_name=supplier_name,
                    ),
                    details=AnomalyDetail(
                        field="unit_price",
                        expected_value=standard_price,
                        actual_value=unit_price,
                        variance_pct=round(variance_pct, 2),
                        tolerance_pct=tolerance_pct,
                    ),
                    description=(
                        f"采购订单 {po_number} 第 {line_number} 行 "
                        f"物料「{material_name}」价格偏差 {variance_pct:.2f}%，"
                        f"合同价 {standard_price:.2f}，实际价 {unit_price:.2f}"
                    ),
                    recommended_action=(
                        "请核实采购单价是否合理，"
                        "确认是否按合同或框架协议价格下单，"
                        "必要时联系采购部门审批价格变更"
                    ),
                    detected_at=datetime.utcnow(),
                    ontology_evidence=f"规则 {_RULE_PRICE_VARIANCE} 触发",
                )
            )

        return anomalies

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _determine_severity(
        self,
        variance_pct: float,
        tolerance_pct: float,
        amount: float,
    ) -> Severity:
        """根据偏差程度和金额判定严重等级。

        规则：
        - 偏差 >= 容差 * 2x 或金额 > 高金额阈值: HIGH
        - 偏差 >= 容差: MEDIUM
        - 接近边界: LOW

        Args:
            variance_pct: 价格偏差百分比。
            tolerance_pct: 容差百分比。
            amount: 涉及金额（单价），用于高金额阈值判断。

        Returns:
            异常严重等级。
        """
        sev_cfg = self._settings.anomaly_severity
        high_multiplier: float = sev_cfg.variance_high_multiplier
        high_amount: float = sev_cfg.high_amount_threshold

        ratio: float = variance_pct / tolerance_pct if tolerance_pct > 0 else float("inf")

        if ratio >= high_multiplier or amount > high_amount:
            return Severity.HIGH
        if ratio >= 1.0:
            return Severity.MEDIUM
        return Severity.LOW

    def _next_anomaly_id(self) -> str:
        """生成下一个异常 ID，格式: ANO-{YYYYMMDD}-{序号}。"""
        self._seq += 1
        today: str = date.today().strftime("%Y%m%d")
        return f"ANO-{today}-{self._seq:04d}"
