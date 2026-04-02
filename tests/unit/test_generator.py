"""模拟数据生成器单元测试。"""

from __future__ import annotations

from modules.p2p.mock_data.generator import MockDataGenerator


class TestMockDataGenerator:
    """MockDataGenerator 测试。"""

    def setup_method(self) -> None:
        self.gen = MockDataGenerator(seed=42)

    def test_generate_suppliers(self) -> None:
        """应生成指定数量的供应商。"""
        suppliers = self.gen.generate_suppliers(5)
        assert len(suppliers) == 5
        assert all("supplier_id" in s for s in suppliers)
        assert all("supplier_name" in s for s in suppliers)

    def test_generate_purchase_orders(self) -> None:
        """应生成 PO 头、行、交货计划行。"""
        suppliers = self.gen.generate_suppliers(3)
        headers, lines, locations = self.gen.generate_purchase_orders(suppliers, 20)
        assert len(headers) == 20
        assert len(lines) == 20
        assert len(locations) == 20
        assert all("po_number" in h for h in headers)

    def test_generate_receipts(self) -> None:
        """应为每个 PO 行生成收货记录。"""
        suppliers = self.gen.generate_suppliers(2)
        _, lines, _ = self.gen.generate_purchase_orders(suppliers, 10)
        rcv_h, rcv_t = self.gen.generate_receipts(lines)
        assert len(rcv_t) == 10

    def test_generate_invoices(self) -> None:
        """应为每个 PO 生成发票。"""
        suppliers = self.gen.generate_suppliers(2)
        headers, lines, _ = self.gen.generate_purchase_orders(suppliers, 10)
        invoices, inv_lines = self.gen.generate_invoices(headers, lines)
        assert len(invoices) == 10
        assert all("invoice_amount" in inv for inv in invoices)

    def test_generate_payments(self) -> None:
        """应为每个发票生成付款记录。"""
        suppliers = self.gen.generate_suppliers(2)
        headers, lines, _ = self.gen.generate_purchase_orders(suppliers, 10)
        invoices, _ = self.gen.generate_invoices(headers, lines)
        payments = self.gen.generate_payments(invoices)
        assert len(payments) == 10

    def test_generate_all(self) -> None:
        """generate_all 应返回完整数据集。"""
        data = self.gen.generate_all()
        expected_keys = {"suppliers", "po_headers", "po_lines", "po_line_locations",
                        "rcv_headers", "rcv_transactions", "invoices", "invoice_lines", "payments"}
        assert set(data.keys()) == expected_keys
        assert len(data["po_headers"]) == 50
        assert len(data["suppliers"]) == 5

    def test_deterministic_with_seed(self) -> None:
        """相同 seed 应生成相同数据。"""
        gen1 = MockDataGenerator(seed=123)
        gen2 = MockDataGenerator(seed=123)
        data1 = gen1.generate_suppliers(3)
        data2 = gen2.generate_suppliers(3)
        assert data1 == data2
