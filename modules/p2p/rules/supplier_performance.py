"""
供应商绩效 KPI 计算模块。

基于采购订单、收货、发票数据计算四个核心 KPI 指标，
并与配置基准值比较生成状态评级。

KPI 指标：
1. otif_rate — 准时足量交付率（On-Time In-Full）
2. invoice_accuracy_rate — 发票准确率
3. quality_pass_rate — 质检合格率
4. price_compliance_rate — 价格合规率
"""

from __future__ import annotations

from typing import Any

from api.schemas.analysis import (
    KPIStatus,
    KPIValue,
    SupplierKPIReport,
)
from config.settings import P2PSettings


class SupplierPerformanceCalculator:
    """供应商绩效 KPI 计算器。

    根据 PO、GR、Invoice 原始数据计算各项 KPI 值，
    与配置中的基准值（benchmarks）比较后确定状态评级。

    KPI 状态评级规则：
        - GOOD: KPI 值 >= 基准值
        - NORMAL: KPI 值 >= 基准值 - 5%
        - BELOW_TARGET: KPI 值 >= 基准值 - 15%
        - CRITICAL: KPI 值 < 基准值 - 15%
    """

    def __init__(self, settings: P2PSettings) -> None:
        """初始化供应商绩效计算器。

        Args:
            settings: P2P 模块配置对象，包含 KPI 基准值。
        """
        self._settings: P2PSettings = settings
        self._benchmarks = settings.supplier_performance

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def calculate(
        self,
        supplier_id: str,
        supplier_name: str,
        po_lines: list[dict[str, Any]],
        gr_lines: list[dict[str, Any]],
        invoices: list[dict[str, Any]],
        period: str,
    ) -> SupplierKPIReport:
        """计算供应商绩效 KPI 报告。

        Args:
            supplier_id: 供应商 ID。
            supplier_name: 供应商名称。
            po_lines: 该供应商的采购订单行列表，每行需包含:
                - po_number (str): 采购订单号
                - required_date (str, 可选): 要求交付日期 (YYYY-MM-DD)
                - po_quantity (float): 订单数量
                - po_amount (float): 订单金额
                - unit_price (float, 可选): 单价
                - contract_price (float, 可选): 合同价
            gr_lines: 该供应商的收货行列表，每行需包含:
                - po_number (str): 关联 PO 号
                - receipt_date (str, 可选): 实际收货日期 (YYYY-MM-DD)
                - gr_quantity (float): 收货数量
                - quality_passed (bool, 可选): 质检是否合格，默认 True
            invoices: 该供应商的发票列表，每行需包含:
                - po_number (str): 关联 PO 号
                - invoice_amount (float): 发票金额
            period: 评估周期描述，如 "2026-Q1" 或 "2026-03"。

        Returns:
            SupplierKPIReport 实例，包含四项 KPI 指标及其评级。
        """
        kpis: dict[str, KPIValue] = {
            "otif_rate": self._calc_otif_rate(po_lines, gr_lines),
            "invoice_accuracy_rate": self._calc_invoice_accuracy_rate(po_lines, invoices),
            "quality_pass_rate": self._calc_quality_pass_rate(gr_lines),
            "price_compliance_rate": self._calc_price_compliance_rate(po_lines),
        }

        return SupplierKPIReport(
            supplier_id=supplier_id,
            supplier_name=supplier_name,
            period=period,
            kpis=kpis,
        )

    # ------------------------------------------------------------------
    # KPI 计算方法
    # ------------------------------------------------------------------

    def _calc_otif_rate(
        self,
        po_lines: list[dict[str, Any]],
        gr_lines: list[dict[str, Any]],
    ) -> KPIValue:
        """计算准时足量交付率 (OTIF - On-Time In-Full)。

        OTIF = 准时且足量的收货行数 / 总 PO 行数 * 100

        判定条件：
        - 准时 (On-Time): receipt_date <= required_date
        - 足量 (In-Full): gr_quantity >= po_quantity

        Args:
            po_lines: 采购订单行列表。
            gr_lines: 收货行列表。

        Returns:
            OTIF KPI 值。
        """
        if not po_lines:
            return self._build_kpi(
                value=0.0,
                benchmark=self._benchmarks.otif_rate,
            )

        # 按 po_number 索引收货数据
        gr_by_po: dict[str, list[dict[str, Any]]] = {}
        for gr in gr_lines:
            gr_by_po.setdefault(gr["po_number"], []).append(gr)

        total = len(po_lines)
        otif_count = 0

        for po in po_lines:
            po_number: str = po["po_number"]
            required_date: str = po.get("required_date", "")
            po_quantity: float = float(po.get("po_quantity", 0))
            matched_grs = gr_by_po.get(po_number, [])

            if not matched_grs:
                continue

            # 汇总收货数量，取最晚收货日期
            total_gr_quantity = sum(float(g.get("gr_quantity", 0)) for g in matched_grs)
            latest_receipt_date = max(
                (g.get("receipt_date", "") for g in matched_grs),
                default="",
            )

            # 足量判断
            in_full = total_gr_quantity >= po_quantity

            # 准时判断（如果没有 required_date 或 receipt_date，视为准时）
            on_time = True
            if required_date and latest_receipt_date:
                on_time = latest_receipt_date <= required_date

            if on_time and in_full:
                otif_count += 1

        rate = (otif_count / total * 100) if total > 0 else 0.0
        return self._build_kpi(value=rate, benchmark=self._benchmarks.otif_rate)

    def _calc_invoice_accuracy_rate(
        self,
        po_lines: list[dict[str, Any]],
        invoices: list[dict[str, Any]],
    ) -> KPIValue:
        """计算发票准确率。

        发票准确率 = 发票金额与 PO 金额偏差在容差内的行数 / 总配对数 * 100

        Args:
            po_lines: 采购订单行列表。
            invoices: 发票列表。

        Returns:
            发票准确率 KPI 值。
        """
        if not po_lines or not invoices:
            return self._build_kpi(
                value=0.0,
                benchmark=self._benchmarks.invoice_accuracy_rate,
            )

        tolerance_pct = self._settings.three_way_match.default_tolerance_pct

        # 按 po_number 索引 PO 金额
        po_amount_map: dict[str, float] = {}
        for po in po_lines:
            po_number = po["po_number"]
            po_amount_map[po_number] = float(po.get("po_amount", 0))

        total_matched = 0
        accurate_count = 0

        for inv in invoices:
            po_number = inv.get("po_number", "")
            po_amount = po_amount_map.get(po_number)
            if po_amount is None:
                continue

            total_matched += 1
            invoice_amount = float(inv.get("invoice_amount", 0))

            if po_amount == 0:
                if invoice_amount == 0:
                    accurate_count += 1
                continue

            variance_pct = abs(invoice_amount - po_amount) / po_amount * 100
            if variance_pct <= tolerance_pct:
                accurate_count += 1

        rate = (accurate_count / total_matched * 100) if total_matched > 0 else 0.0
        return self._build_kpi(
            value=rate,
            benchmark=self._benchmarks.invoice_accuracy_rate,
        )

    def _calc_quality_pass_rate(
        self,
        gr_lines: list[dict[str, Any]],
    ) -> KPIValue:
        """计算质检合格率。

        质检合格率 = 质检合格的收货行数 / 总收货行数 * 100

        每条 GR 行通过 ``quality_passed`` 字段标识是否合格，默认为 True。

        Args:
            gr_lines: 收货行列表。

        Returns:
            质检合格率 KPI 值。
        """
        if not gr_lines:
            return self._build_kpi(
                value=0.0,
                benchmark=self._benchmarks.quality_pass_rate,
            )

        total = len(gr_lines)
        passed = sum(
            1 for gr in gr_lines if gr.get("quality_passed", True)
        )

        rate = (passed / total * 100) if total > 0 else 0.0
        return self._build_kpi(value=rate, benchmark=self._benchmarks.quality_pass_rate)

    def _calc_price_compliance_rate(
        self,
        po_lines: list[dict[str, Any]],
    ) -> KPIValue:
        """计算价格合规率。

        价格合规率 = 单价偏差在容差内的 PO 行数 / 有合同价的总行数 * 100

        通过比较 ``unit_price`` 与 ``contract_price`` 计算偏差。

        Args:
            po_lines: 采购订单行列表。

        Returns:
            价格合规率 KPI 值。
        """
        tolerance_pct = self._settings.three_way_match.default_tolerance_pct
        total_with_contract = 0
        compliant_count = 0

        for po in po_lines:
            unit_price = po.get("unit_price")
            contract_price = po.get("contract_price")

            if unit_price is None or contract_price is None:
                continue
            unit_price = float(unit_price)
            contract_price = float(contract_price)

            if contract_price == 0:
                continue

            total_with_contract += 1
            variance_pct = abs(unit_price - contract_price) / contract_price * 100
            if variance_pct <= tolerance_pct:
                compliant_count += 1

        rate = (
            (compliant_count / total_with_contract * 100)
            if total_with_contract > 0
            else 100.0
        )
        return self._build_kpi(
            value=rate,
            benchmark=self._benchmarks.price_compliance_rate,
        )

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _build_kpi(self, *, value: float, benchmark: float) -> KPIValue:
        """构建 KPIValue 并确定状态评级。

        评级规则：
        - value >= benchmark: GOOD
        - value >= benchmark - 5: NORMAL
        - value >= benchmark - 15: BELOW_TARGET
        - value < benchmark - 15: CRITICAL

        Args:
            value: KPI 实际值。
            benchmark: 基准值。

        Returns:
            KPIValue 实例。
        """
        status = self._evaluate_status(value, benchmark)
        return KPIValue(
            value=round(value, 2),
            unit="%",
            benchmark=benchmark,
            status=status,
        )

    @staticmethod
    def _evaluate_status(value: float, benchmark: float) -> KPIStatus:
        """根据实际值与基准值的差距确定 KPI 状态。

        Args:
            value: KPI 实际值。
            benchmark: KPI 基准值。

        Returns:
            KPI 状态枚举值。
        """
        if value >= benchmark:
            return KPIStatus.GOOD
        if value >= benchmark - 5.0:
            return KPIStatus.NORMAL
        if value >= benchmark - 15.0:
            return KPIStatus.BELOW_TARGET
        return KPIStatus.CRITICAL
