"""
P2P 模拟数据生成器。

按 Oracle EBS 标准表结构生成包含正常和异常数据的测试数据集。
异常率接近真实生产环境（5-10%），用于 MVP 阶段验证分析逻辑。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any


class MockDataGenerator:
    """P2P 模拟数据生成器，按 Oracle EBS 表结构生成测试数据。"""

    def __init__(self, seed: int = 42) -> None:
        """初始化生成器。seed 确保数据可重复。"""
        self._rng = random.Random(seed)
        self._base_date = datetime(2024, 3, 1)

    def _rand_date(self, start_offset: int = 0, end_offset: int = 90) -> str:
        """生成随机日期字符串（ISO 格式）。"""
        delta = self._rng.randint(start_offset, end_offset)
        return (self._base_date + timedelta(days=delta)).strftime("%Y-%m-%d")

    def generate_suppliers(self, count: int = 5) -> list[dict[str, Any]]:
        """生成供应商主数据（AP_SUPPLIERS + AP_SUPPLIER_SITES_ALL）。"""
        names = ["华为科技", "中兴通讯", "比亚迪电子", "联想集团", "海尔智家",
                 "格力电器", "美的集团", "小米科技", "大疆创新", "宁德时代"]
        terms = ["NET30", "NET45", "NET60", "2/10NET30"]
        suppliers = []
        for i in range(count):
            suppliers.append({
                "supplier_id": f"SUP-{i + 1:03d}",
                "supplier_name": names[i % len(names)],
                "supplier_site_id": f"SITE-{i + 1:03d}",
                "payment_terms": self._rng.choice(terms),
                "status": "ACTIVE",
            })
        return suppliers

    def generate_purchase_orders(
        self,
        suppliers: list[dict[str, Any]],
        count: int = 50,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """
        生成采购订单数据。

        Returns:
            (po_headers, po_lines, po_line_locations) 三元组。
        """
        items = [
            ("MAT-001", "钢板", "RAW_MATERIAL", 150.0),
            ("MAT-002", "铜线", "RAW_MATERIAL", 85.0),
            ("CMP-001", "电路板", "COMPONENT", 320.0),
            ("CMP-002", "电容器", "COMPONENT", 12.5),
            ("PKG-001", "包装箱", "PACKAGING", 8.0),
        ]
        headers: list[dict[str, Any]] = []
        lines: list[dict[str, Any]] = []
        locations: list[dict[str, Any]] = []

        for i in range(count):
            sup = self._rng.choice(suppliers)
            item = self._rng.choice(items)
            qty = self._rng.randint(100, 5000)
            unit_price = item[3]
            # ~5% 的行标准价与实际价不同（价格差异异常）
            if self._rng.random() < 0.05:
                standard_price = unit_price
                unit_price = round(unit_price * self._rng.uniform(1.06, 1.15), 2)
            else:
                standard_price = unit_price
            amount = round(qty * unit_price, 2)
            po_num = f"PO-2024-{i + 1:04d}"
            creation = self._rand_date(0, 60)

            headers.append({
                "po_header_id": i + 1,
                "po_number": po_num,
                "supplier_id": sup["supplier_id"],
                "supplier_name": sup["supplier_name"],
                "status": "APPROVED",
                "creation_date": creation,
                "total_amount": amount,
                "currency": "CNY",
            })
            lines.append({
                "po_line_id": i + 1,
                "po_header_id": i + 1,
                "po_number": po_num,
                "line_num": 1,
                "item_code": item[0],
                "item_description": item[1],
                "quantity": qty,
                "unit_price": unit_price,
                "amount": amount,
                "category": item[2],
                "standard_price": standard_price,
            })
            promised = self._rand_date(30, 75)
            locations.append({
                "line_location_id": i + 1,
                "po_line_id": i + 1,
                "po_number": po_num,
                "promised_date": promised,
                "need_by_date": promised,
                "quantity": qty,
            })
        return headers, lines, locations

    def generate_receipts(
        self,
        po_lines: list[dict[str, Any]],
        anomaly_rate: float = 0.08,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        生成收货数据（RCV_SHIPMENT_HEADERS + RCV_TRANSACTIONS）。

        anomaly_rate 控制数量偏差和交货延迟的比例。
        """
        headers: list[dict[str, Any]] = []
        transactions: list[dict[str, Any]] = []
        for idx, pl in enumerate(po_lines):
            qty = pl["quantity"]
            # 异常：收货数量偏差
            if self._rng.random() < anomaly_rate:
                rcv_qty = int(qty * self._rng.uniform(0.80, 0.94))
                rejected = int(qty * self._rng.uniform(0.02, 0.05))
            else:
                rcv_qty = qty
                rejected = 0
            accepted = rcv_qty - rejected
            rcv_date = self._rand_date(35, 80)
            headers.append({
                "shipment_header_id": idx + 1,
                "receipt_num": f"RCV-2024-{idx + 1:04d}",
                "supplier_id": pl.get("supplier_id", ""),
                "creation_date": rcv_date,
            })
            transactions.append({
                "transaction_id": idx + 1,
                "shipment_header_id": idx + 1,
                "po_number": pl["po_number"],
                "po_line_id": pl["po_line_id"],
                "transaction_type": "RECEIVE",
                "quantity": rcv_qty,
                "accepted_quantity": accepted,
                "rejected_quantity": rejected,
                "transaction_date": rcv_date,
                "supplier_id": pl.get("supplier_id", ""),
            })
        return headers, transactions

    def generate_invoices(
        self,
        po_headers: list[dict[str, Any]],
        po_lines: list[dict[str, Any]],
        anomaly_rate: float = 0.08,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        生成发票数据（AP_INVOICES_ALL + AP_INVOICE_LINES_ALL）。

        anomaly_rate 控制金额偏差的比例。
        """
        invoices: list[dict[str, Any]] = []
        inv_lines: list[dict[str, Any]] = []
        for idx, (ph, pl) in enumerate(zip(po_headers, po_lines)):
            po_amount = ph["total_amount"]
            # 异常：发票金额偏差超容差
            if self._rng.random() < anomaly_rate:
                inv_amount = round(po_amount * self._rng.uniform(1.06, 1.15), 2)
            else:
                inv_amount = po_amount
            inv_date = self._rand_date(40, 85)
            due_date_dt = datetime.strptime(inv_date, "%Y-%m-%d") + timedelta(days=30)
            disc_due_dt = datetime.strptime(inv_date, "%Y-%m-%d") + timedelta(days=10)
            inv_num = f"INV-2024-{idx + 1:04d}"
            invoices.append({
                "invoice_id": idx + 1,
                "invoice_number": inv_num,
                "po_number": ph["po_number"],
                "supplier_id": ph["supplier_id"],
                "supplier_name": ph["supplier_name"],
                "invoice_amount": inv_amount,
                "invoice_date": inv_date,
                "due_date": due_date_dt.strftime("%Y-%m-%d"),
                "discount_due_date": disc_due_dt.strftime("%Y-%m-%d"),
                "status": "VALIDATED",
                "payment_terms": "NET30",
            })
            inv_lines.append({
                "invoice_line_id": idx + 1,
                "invoice_id": idx + 1,
                "invoice_number": inv_num,
                "po_number": ph["po_number"],
                "line_num": 1,
                "amount": inv_amount,
                "quantity": pl["quantity"],
            })
        return invoices, inv_lines

    def generate_payments(
        self,
        invoices: list[dict[str, Any]],
        anomaly_rate: float = 0.10,
    ) -> list[dict[str, Any]]:
        """
        生成付款数据（AP_PAYMENTS_ALL）。

        anomaly_rate 控制逾期/提前付款/折扣滥用的比例。
        """
        payments: list[dict[str, Any]] = []
        for idx, inv in enumerate(invoices):
            due = datetime.strptime(inv["due_date"], "%Y-%m-%d")
            inv_amount = inv["invoice_amount"]
            roll = self._rng.random()

            if roll < anomaly_rate * 0.5:
                # 逾期付款
                pay_date = due + timedelta(days=self._rng.randint(5, 60))
                pay_amount = inv_amount
            elif roll < anomaly_rate:
                # 提前付款过早
                pay_date = due - timedelta(days=self._rng.randint(15, 30))
                pay_amount = inv_amount
            elif roll < anomaly_rate + 0.03:
                # 折扣滥用：过了折扣期仍按折扣价付
                disc_due = datetime.strptime(inv.get("discount_due_date", inv["due_date"]), "%Y-%m-%d")
                pay_date = disc_due + timedelta(days=self._rng.randint(3, 15))
                pay_amount = round(inv_amount * 0.98, 2)  # 按2%折扣付
            else:
                # 正常付款
                pay_date = due - timedelta(days=self._rng.randint(1, 5))
                pay_amount = inv_amount

            payments.append({
                "payment_id": idx + 1,
                "payment_number": f"PAY-2024-{idx + 1:04d}",
                "invoice_number": inv["invoice_number"],
                "supplier_id": inv["supplier_id"],
                "payment_amount": pay_amount,
                "payment_date": pay_date.strftime("%Y-%m-%d"),
                "payment_method": self._rng.choice(["BANK_TRANSFER", "CHECK"]),
            })
        return payments

    def generate_all(self) -> dict[str, list[dict[str, Any]]]:
        """
        一键生成完整 P2P 数据集。

        Returns:
            包含所有表数据的字典。
        """
        suppliers = self.generate_suppliers()
        po_headers, po_lines, po_locations = self.generate_purchase_orders(suppliers)
        # 补充 supplier_id 到 po_lines（收货生成需要）
        line_sup_map = {h["po_number"]: h["supplier_id"] for h in po_headers}
        for pl in po_lines:
            pl["supplier_id"] = line_sup_map.get(pl["po_number"], "")
        rcv_headers, rcv_transactions = self.generate_receipts(po_lines)
        invoices, invoice_lines = self.generate_invoices(po_headers, po_lines)
        payments = self.generate_payments(invoices)
        return {
            "suppliers": suppliers,
            "po_headers": po_headers,
            "po_lines": po_lines,
            "po_line_locations": po_locations,
            "rcv_headers": rcv_headers,
            "rcv_transactions": rcv_transactions,
            "invoices": invoices,
            "invoice_lines": invoice_lines,
            "payments": payments,
        }
