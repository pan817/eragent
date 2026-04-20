"""
P2P 模拟数据生成器。

按 Oracle EBS 标准表结构生成包含正常和异常数据的测试数据集。
异常率接近真实生产环境（5-10%），用于验证分析逻辑。

日期分布策略：
- 60% 数据：2026-01-01 ~ 今天（近期，30d/90d 查询可命中）
- 25% 数据：2025 年（历史，365d 查询可命中）
- 15% 数据：2024 年及更早（早期数据）
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any


class MockDataGenerator:
    """P2P 模拟数据生成器，按 Oracle EBS 表结构生成测试数据。"""

    def __init__(self, seed: int = 42) -> None:
        """初始化生成器。seed 确保数据可重复。"""
        self._rng = random.Random(seed)
        self._today = date.today()

    def _rand_creation_date(self) -> date:
        """按 60/25/15 分布生成 PO 创建日期。

        近期数据（60%）的上限为今天 - 60 天，确保后续单据
        （收货 +25~50 天、发票 +30~55 天、付款基于到期日）
        不会产生超过今天的日期。
        """
        roll = self._rng.random()
        if roll < 0.60:
            # 60%：2026-01-01 ~ 今天-60天（预留后续单据偏移空间）
            start = date(2026, 1, 1)
            end = self._today - timedelta(days=60)
            if end <= start:
                end = start
            span = max((end - start).days, 1)
            return start + timedelta(days=self._rng.randint(0, span))
        elif roll < 0.85:
            # 25%：2025 年
            start = date(2025, 1, 1)
            return start + timedelta(days=self._rng.randint(0, 364))
        else:
            # 15%：2024 年
            start = date(2024, 1, 1)
            return start + timedelta(days=self._rng.randint(0, 364))

    def _offset_date(self, base: date, min_days: int, max_days: int, rng: random.Random) -> date:
        """基于基准日期偏移随机天数，不超过今天。"""
        result = base + timedelta(days=rng.randint(min_days, max_days))
        return min(result, self._today)

    @staticmethod
    def _fmt(d: date) -> str:
        return d.strftime("%Y-%m-%d")

    def generate_suppliers(self, count: int = 5) -> list[dict[str, Any]]:
        """生成供应商主数据（AP_SUPPLIERS）。"""
        names = ["华为科技", "中兴通讯", "比亚迪电子", "联想集团", "海尔智家",
                 "格力电器", "美的集团", "小米科技", "大疆创新", "宁德时代"]
        terms = ["NET30", "NET45", "NET60", "2/10NET30"]
        industry_classes = ["3672", "3661", "3694", "3571", "3631"]
        vendor_types = ["EMPLOYEE", "VENDOR", "CONTRACTOR"]
        suppliers = []
        for i in range(count):
            start_active = date(2020, 1, 1) + timedelta(days=i * 60)
            suppliers.append({
                "vendor_id": f"SUP-{i + 1:03d}",
                "vendor_name": names[i % len(names)],
                "supplier_site_id": f"SITE-{i + 1:03d}",
                "terms_id": self._rng.choice(terms),
                "enabled_flag": "Y",
                "segment1": f"SUP-{i + 1:03d}",
                "vendor_type_lookup_code": vendor_types[i % len(vendor_types)],
                "start_date_active": self._fmt(start_active),
                "standard_industry_class": industry_classes[i % len(industry_classes)],
                "last_update_date": self._fmt(start_active),
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
            creation = self._rand_creation_date()

            headers.append({
                "po_header_id": i + 1,
                "po_number": po_num,
                "vendor_id": sup["vendor_id"],
                "vendor_name": sup["vendor_name"],
                "status": "APPROVED",
                "creation_date": self._fmt(creation),
                "total_amount": amount,
                "currency": "CNY",
                "type_lookup_code": "STANDARD",
                "authorization_status": "APPROVED",
                "buyer_id": 1001,
                "org_id": 1,
                "last_update_date": self._fmt(creation),
                "_creation_date_obj": creation,  # 内部用，不入库
            })
            lines.append({
                "po_line_id": i + 1,
                "po_header_id": i + 1,
                "po_number": po_num,
                "line_num": 1,
                "item_id": item[0],
                "item_description": item[1],
                "quantity": qty,
                "unit_price": unit_price,
                "amount": amount,
                "category_id": item[2],
                "standard_price": standard_price,
                "unit_meas_lookup_code": "EA",
                "last_update_date": self._fmt(creation),
            })
            promised = self._offset_date(creation, 20, 45, self._rng)
            locations.append({
                "line_location_id": i + 1,
                "po_line_id": i + 1,
                "po_number": po_num,
                "promised_date": self._fmt(promised),
                "need_by_date": self._fmt(promised),
                "quantity": qty,
                "last_update_date": self._fmt(creation),
            })
        return headers, lines, locations

    def generate_receipts(
        self,
        po_lines: list[dict[str, Any]],
        po_headers: list[dict[str, Any]],
        anomaly_rate: float = 0.08,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        生成收货数据（RCV_SHIPMENT_HEADERS + RCV_TRANSACTIONS）。

        anomaly_rate 控制数量偏差和交货延迟的比例。
        """
        # 建立 po_number → creation_date 映射
        creation_map = {h["po_number"]: h["_creation_date_obj"] for h in po_headers}

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
            base_date = creation_map.get(pl["po_number"], self._today)
            rcv_date = self._offset_date(base_date, 25, 50, self._rng)
            shipped_date = self._offset_date(base_date, 20, 40, self._rng)
            expected_rcv = self._offset_date(base_date, 22, 45, self._rng)
            headers.append({
                "shipment_header_id": idx + 1,
                "receipt_num": f"RCV-2024-{idx + 1:04d}",
                "vendor_id": pl.get("vendor_id", ""),
                "creation_date": self._fmt(rcv_date),
                "shipped_date": self._fmt(shipped_date),
                "expected_receipt_date": self._fmt(expected_rcv),
                "last_update_date": self._fmt(rcv_date),
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
                "transaction_date": self._fmt(rcv_date),
                "vendor_id": pl.get("vendor_id", ""),
                "source_document_code": "PO",
                "last_update_date": self._fmt(rcv_date),
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
            base_date = ph["_creation_date_obj"]
            # 异常：发票金额偏差超容差
            if self._rng.random() < anomaly_rate:
                inv_amount = round(po_amount * self._rng.uniform(1.06, 1.15), 2)
            else:
                inv_amount = po_amount
            inv_date = self._offset_date(base_date, 30, 55, self._rng)
            due_date = min(inv_date + timedelta(days=30), self._today)
            disc_due = min(inv_date + timedelta(days=10), self._today)
            inv_num = f"INV-2024-{idx + 1:04d}"
            invoices.append({
                "invoice_id": idx + 1,
                "invoice_num": inv_num,
                "po_number": ph["po_number"],
                "vendor_id": ph["vendor_id"],
                "vendor_name": ph["vendor_name"],
                "invoice_amount": inv_amount,
                "invoice_date": self._fmt(inv_date),
                "due_date": self._fmt(due_date),
                "discount_due_date": self._fmt(disc_due),
                "approval_status": "VALIDATED",
                "terms_id": "NET30",
                "invoice_type_lookup_code": "STANDARD",
                "invoice_currency_code": "CNY",
                "last_update_date": self._fmt(inv_date),
            })
            inv_lines.append({
                "invoice_line_id": idx + 1,
                "invoice_id": idx + 1,
                "invoice_num": inv_num,
                "po_number": ph["po_number"],
                "line_num": 1,
                "amount": inv_amount,
                "quantity": pl["quantity"],
                "line_number": 1,
                "line_type_lookup_code": "ITEM",
                "accounting_date": self._fmt(inv_date),
                "last_update_date": self._fmt(inv_date),
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
            due = datetime.strptime(inv["due_date"], "%Y-%m-%d").date()
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
                disc_due = datetime.strptime(
                    inv.get("discount_due_date", inv["due_date"]), "%Y-%m-%d"
                ).date()
                pay_date = disc_due + timedelta(days=self._rng.randint(3, 15))
                pay_amount = round(inv_amount * 0.98, 2)  # 按2%折扣付
            else:
                # 正常付款
                pay_date = due - timedelta(days=self._rng.randint(1, 5))
                pay_amount = inv_amount

            pay_date = min(pay_date, self._today)

            payments.append({
                "check_id": idx + 1,
                "check_number": f"PAY-2024-{idx + 1:04d}",
                "invoice_num": inv["invoice_num"],
                "vendor_id": inv["vendor_id"],
                "amount": pay_amount,
                "check_date": self._fmt(pay_date),
                "payment_method_code": self._rng.choice(["BANK_TRANSFER", "CHECK"]),
                "status_lookup_code": "NEGOTIABLE",
                "currency_code": "CNY",
                "last_update_date": self._fmt(pay_date),
            })
        return payments

    def generate_supplier_sites(
        self,
        suppliers: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """生成供应商地点数据（AP_SUPPLIER_SITES_ALL）。每个供应商一个地点。"""
        cities = ["深圳", "北京", "上海", "广州", "杭州"]
        sites: list[dict[str, Any]] = []
        for i, sup in enumerate(suppliers):
            sites.append({
                "vendor_site_id": i + 1,
                "vendor_id": sup["vendor_id"],
                "vendor_site_code": f"SITE-{i + 1:03d}",
                "address_line1": f"科技园路{i + 1}号",
                "city": cities[i % len(cities)],
                "country": "CN",
                "phone": f"0755-{80000000 + i}",
                "email_address": f"contact{i + 1}@supplier{i + 1}.com",
                "purchasing_site_flag": "Y",
                "pay_site_flag": "Y",
                "org_id": 1,
                "last_update_date": sup["last_update_date"],
            })
        return sites

    def generate_materials(self) -> list[dict[str, Any]]:
        """生成物料主数据（MTL_SYSTEM_ITEMS_B）。"""
        items = [
            ("MAT-001", "钢板", "RAW_MATERIAL", 150.0),
            ("MAT-002", "铜线", "RAW_MATERIAL", 85.0),
            ("CMP-001", "电路板", "COMPONENT", 320.0),
            ("CMP-002", "电容器", "COMPONENT", 12.5),
            ("PKG-001", "包装箱", "PACKAGING", 8.0),
        ]
        materials: list[dict[str, Any]] = []
        for i, (seg, desc, item_type, price) in enumerate(items):
            materials.append({
                "inventory_item_id": i + 1,
                "organization_id": 1,
                "segment1": seg,
                "description": desc,
                "primary_uom_code": "EA",
                "item_type": item_type,
                "list_price_per_unit": price,
                "purchasing_item_flag": "Y",
                "purchasing_enabled_flag": "Y",
                "inventory_item_status_code": "Active",
            })
        return materials

    def generate_po_distributions(
        self,
        po_lines: list[dict[str, Any]],
        po_locations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """生成 PO 分配数据（PO_DISTRIBUTIONS_ALL）。每行一条分配。"""
        distributions: list[dict[str, Any]] = []
        for idx, (pl, loc) in enumerate(zip(po_lines, po_locations)):
            distributions.append({
                "po_distribution_id": idx + 1,
                "po_header_id": pl["po_header_id"],
                "po_line_id": pl["po_line_id"],
                "line_location_id": loc["line_location_id"],
                "quantity_ordered": pl["quantity"],
                "destination_type_code": "EXPENSE",
                "org_id": 1,
            })
        return distributions

    def generate_shipment_lines(
        self,
        rcv_headers: list[dict[str, Any]],
        po_lines: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """生成收货单行数据（RCV_SHIPMENT_LINES）。每个收货单一行。"""
        shipment_lines: list[dict[str, Any]] = []
        for idx, (rh, pl) in enumerate(zip(rcv_headers, po_lines)):
            shipment_lines.append({
                "shipment_line_id": idx + 1,
                "shipment_header_id": rh["shipment_header_id"],
                "line_num": 1,
                "po_header_id": pl["po_header_id"],
                "po_line_id": pl["po_line_id"],
                "item_id": idx + 1,
                "quantity_shipped": pl["quantity"],
                "quantity_received": pl["quantity"],
                "unit_of_measure": "EA",
            })
        return shipment_lines

    def generate_invoice_distributions(
        self,
        invoices: list[dict[str, Any]],
        invoice_lines: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """生成发票分配数据（AP_INVOICE_DISTRIBUTIONS_ALL）。每张发票一条分配。"""
        distributions: list[dict[str, Any]] = []
        for idx, (inv, il) in enumerate(zip(invoices, invoice_lines)):
            distributions.append({
                "invoice_distribution_id": idx + 1,
                "invoice_id": inv["invoice_id"],
                "invoice_line_number": 1,
                "distribution_line_number": 1,
                "amount": il["amount"],
                "accounting_date": inv["invoice_date"],
                "match_status_flag": "A",
                "posted_flag": "Y",
            })
        return distributions

    def generate_invoice_payments(
        self,
        invoices: list[dict[str, Any]],
        payments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """生成发票付款关联数据（AP_INVOICE_PAYMENTS_ALL）。每笔付款一条关联。"""
        inv_payments: list[dict[str, Any]] = []
        for idx, (inv, pay) in enumerate(zip(invoices, payments)):
            inv_payments.append({
                "invoice_payment_id": idx + 1,
                "invoice_id": inv["invoice_id"],
                "check_id": pay["check_id"],
                "payment_num": 1,
                "amount": pay["amount"],
                "accounting_date": pay["check_date"],
            })
        return inv_payments

    def generate_payment_schedules(
        self,
        invoices: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """生成付款计划数据（AP_PAYMENT_SCHEDULES_ALL）。每张发票一条计划。"""
        schedules: list[dict[str, Any]] = []
        for idx, inv in enumerate(invoices):
            schedules.append({
                "payment_schedule_id": idx + 1,
                "invoice_id": inv["invoice_id"],
                "payment_num": 1,
                "due_date": inv["due_date"],
                "discount_date": inv.get("discount_due_date", inv["due_date"]),
                "gross_amount": inv["invoice_amount"],
                "amount_remaining": 0,
                "payment_status_flag": "Y",
            })
        return schedules

    def generate_sourcing(
        self,
        suppliers: list[dict[str, Any]],
        count: int = 5,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """生成寻源/合同数据（PON_AUCTION_HEADERS, PON_BID_HEADERS, OKC_K_HEADERS, OKC_K_LINES）。"""
        auctions: list[dict[str, Any]] = []
        bids: list[dict[str, Any]] = []
        contracts: list[dict[str, Any]] = []
        contract_lines: list[dict[str, Any]] = []

        bid_id = 1
        auction_types = ["SEALED_BID", "REVERSE_AUCTION", "RFQ"]
        for i in range(count):
            open_dt = date(2025, 6, 1) + timedelta(days=i * 30)
            close_dt = open_dt + timedelta(days=14)
            auctions.append({
                "auction_header_id": i + 1,
                "document_number": f"AUC-{i + 1:03d}",
                "auction_title": f"Sourcing Event {i + 1}",
                "auction_type": auction_types[i % len(auction_types)],
                "auction_status": "AUCTION_CLOSED",
                "open_bidding_date": self._fmt(open_dt),
                "close_bidding_date": self._fmt(close_dt),
                "outcome": "AWARDED",
                "org_id": 1,
            })
            # 2-3 bids per auction
            num_bids = 2 if i % 2 == 0 else 3
            for b in range(num_bids):
                sup = suppliers[b % len(suppliers)]
                bids.append({
                    "bid_number": bid_id,
                    "auction_header_id": i + 1,
                    "bid_status": "ACTIVE",
                    "vendor_id": b + 1,
                    "bid_total": round(self._rng.uniform(50000, 200000), 2),
                    "bid_currency_code": "CNY",
                    "publish_date": self._fmt(open_dt + timedelta(days=1)),
                    "award_status": "AWARDED" if b == 0 else "REJECTED",
                    "award_date": self._fmt(close_dt) if b == 0 else None,
                })
                bid_id += 1

        # 3 contracts with 2 lines each
        contract_line_id = 1
        items_for_contract = [
            (1, "钢板", 145.0),
            (2, "铜线", 82.0),
            (3, "电路板", 310.0),
        ]
        for i in range(3):
            start = date(2025, 1, 1) + timedelta(days=i * 120)
            end = start + timedelta(days=365)
            contracts.append({
                "id": i + 1,
                "contract_number": f"CNT-{i + 1:03d}",
                "sts_code": "ACTIVE",
                "start_date": self._fmt(start),
                "end_date": self._fmt(end),
                "estimated_amount": round(self._rng.uniform(500000, 2000000), 2),
                "currency_code": "CNY",
                "authoring_org_id": 1,
                "buy_or_sell": "B",
                "description": f"Purchase Contract {i + 1}",
            })
            item_id, item_desc, price = items_for_contract[i]
            for ln in range(1, 3):
                contract_lines.append({
                    "id": contract_line_id,
                    "chr_id": i + 1,
                    "line_number": str(ln),
                    "sts_code": "ACTIVE",
                    "start_date": self._fmt(start),
                    "end_date": self._fmt(end),
                    "item_id": item_id,
                    "item_description": item_desc,
                    "price_unit": price,
                    "price_negotiated": round(price * 0.95, 2),
                    "quantity": self._rng.randint(1000, 5000),
                    "uom_code": "EA",
                })
                contract_line_id += 1

        return auctions, bids, contracts, contract_lines

    def generate_all(self, count: int = 50) -> dict[str, list[dict[str, Any]]]:
        """
        一键生成完整 P2P 数据集。

        Args:
            count: 生成的采购订单数量（发票、付款等同步生成相同数量）。

        Returns:
            包含所有表数据的字典。
        """
        suppliers = self.generate_suppliers()
        supplier_sites = self.generate_supplier_sites(suppliers)
        materials = self.generate_materials()
        po_headers, po_lines, po_locations = self.generate_purchase_orders(suppliers, count=count)
        # 补充 vendor_id 到 po_lines（收货生成需要）
        line_sup_map = {h["po_number"]: h["vendor_id"] for h in po_headers}
        for pl in po_lines:
            pl["vendor_id"] = line_sup_map.get(pl["po_number"], "")
        po_distributions = self.generate_po_distributions(po_lines, po_locations)
        rcv_headers, rcv_transactions = self.generate_receipts(po_lines, po_headers)
        shipment_lines = self.generate_shipment_lines(rcv_headers, po_lines)
        invoices, invoice_lines = self.generate_invoices(po_headers, po_lines)
        invoice_distributions = self.generate_invoice_distributions(invoices, invoice_lines)
        payments = self.generate_payments(invoices)
        invoice_payments = self.generate_invoice_payments(invoices, payments)
        payment_schedules = self.generate_payment_schedules(invoices)
        auctions, auction_bids, contracts, contract_lines = self.generate_sourcing(suppliers)

        # 清理内部字段
        for h in po_headers:
            h.pop("_creation_date_obj", None)

        return {
            "suppliers": suppliers,
            "supplier_sites": supplier_sites,
            "materials": materials,
            "po_headers": po_headers,
            "po_lines": po_lines,
            "po_line_locations": po_locations,
            "po_distributions": po_distributions,
            "rcv_headers": rcv_headers,
            "rcv_transactions": rcv_transactions,
            "shipment_lines": shipment_lines,
            "invoices": invoices,
            "invoice_lines": invoice_lines,
            "invoice_distributions": invoice_distributions,
            "payments": payments,
            "invoice_payments": invoice_payments,
            "payment_schedules": payment_schedules,
            "auctions": auctions,
            "bids": auction_bids,
            "contracts": contracts,
            "contract_lines": contract_lines,
        }
