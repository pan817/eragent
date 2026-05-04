"""P2P Repository 单元测试（基于 SQLite 内存库）。"""

from __future__ import annotations

from modules.p2p.repository import P2PRepository


class TestQueryMethods:
    """查询方法测试。"""

    def test_query_purchase_orders_all(self, repository: P2PRepository) -> None:
        """无过滤条件应返回所有 PO。"""
        pos = repository.query_purchase_orders(days=0)
        assert len(pos) == 50
        assert all("po_number" in po for po in pos)
        assert all("vendor_id" in po for po in pos)

    def test_query_purchase_orders_by_supplier(self, repository: P2PRepository) -> None:
        """按供应商过滤应只返回该供应商的 PO。"""
        pos = repository.query_purchase_orders(vendor_id="SUP-001", days=0)
        assert len(pos) > 0
        assert all(po["vendor_id"] == "SUP-001" for po in pos)

    def test_query_receipts_all(self, repository: P2PRepository) -> None:
        """无过滤条件应返回所有收货记录。"""
        receipts = repository.query_receipts(days=0)
        assert len(receipts) == 50
        assert all("gr_number" in r for r in receipts)

    def test_query_receipts_by_po(self, repository: P2PRepository) -> None:
        """按 PO 号过滤。"""
        receipts = repository.query_receipts(po_number="PO-2024-0001", days=0)
        assert len(receipts) >= 1
        assert all(r["po_number"] == "PO-2024-0001" for r in receipts)

    def test_query_invoices_all(self, repository: P2PRepository) -> None:
        """无过滤条件应返回所有发票。"""
        invoices = repository.query_invoices(days=0)
        assert len(invoices) == 50

    def test_query_invoices_by_supplier(self, repository: P2PRepository) -> None:
        """按供应商过滤。"""
        invoices = repository.query_invoices(vendor_id="SUP-002", days=0)
        assert len(invoices) > 0
        assert all(inv["vendor_id"] == "SUP-002" for inv in invoices)

    def test_query_payments_all(self, repository: P2PRepository) -> None:
        """无过滤条件应返回所有付款记录。"""
        payments = repository.query_payments(days=0)
        assert len(payments) == 50

    def test_query_payments_by_invoice(self, repository: P2PRepository) -> None:
        """按发票号过滤。"""
        payments = repository.query_payments(invoice_num="INV-2024-0001", days=0)
        assert len(payments) >= 1
        assert all(p["invoice_num"] == "INV-2024-0001" for p in payments)


class TestLimitAndOrderBy:
    """limit + order_by 参数组合测试。"""

    def test_order_by_date_desc(self, repository: P2PRepository) -> None:
        pos = repository.query_purchase_orders(days=0, limit=5, order_by="date_desc")
        assert len(pos) == 5
        dates = [po["creation_date"] for po in pos]
        assert dates == sorted(dates, reverse=True)

    def test_order_by_amount_desc(self, repository: P2PRepository) -> None:
        pos = repository.query_purchase_orders(days=0, limit=5, order_by="amount_desc")
        assert len(pos) == 5
        amounts = [po["po_amount"] for po in pos]
        assert amounts == sorted(amounts, reverse=True)

    def test_limit_returns_exact_count(self, repository: P2PRepository) -> None:
        for limit in (1, 5, 10):
            pos = repository.query_purchase_orders(days=0, limit=limit)
            assert len(pos) == limit, f"limit={limit} returned {len(pos)}"

    def test_limit_zero_means_no_limit(self, repository: P2PRepository) -> None:
        pos = repository.query_purchase_orders(days=0, limit=0)
        assert len(pos) == 50

    def test_days_filter_narrow_window(self, repository: P2PRepository) -> None:
        pos_all = repository.query_purchase_orders(days=0)
        pos_1d = repository.query_purchase_orders(days=1)
        assert len(pos_1d) <= len(pos_all)


class TestFlattenedMethods:
    """扁平化数据方法测试（供规则引擎使用）。"""

    def test_flattened_purchase_orders_format(self, repository: P2PRepository) -> None:
        """扁平化 PO 数据应包含规则引擎所需的所有字段。"""
        pos = repository.get_flattened_purchase_orders()
        assert len(pos) > 0
        required_keys = {
            "po_number", "vendor_id", "vendor_name", "material_category",
            "po_amount", "po_quantity", "unit_price", "contract_price",
            "status", "creation_date", "required_date", "material_code",
            "material_name", "line_number",
        }
        for po in pos:
            assert required_keys.issubset(po.keys()), f"缺少字段: {required_keys - po.keys()}"

    def test_flattened_purchase_orders_by_po_number(self, repository: P2PRepository) -> None:
        """按 PO 号过滤。"""
        pos = repository.get_flattened_purchase_orders(po_number="PO-2024-0001")
        assert len(pos) == 1
        assert pos[0]["po_number"] == "PO-2024-0001"

    def test_flattened_receipts_format(self, repository: P2PRepository) -> None:
        """扁平化收货数据格式正确。"""
        receipts = repository.get_flattened_receipts()
        assert len(receipts) > 0
        required_keys = {"gr_number", "po_number", "gr_quantity", "receipt_date", "quality_passed"}
        for r in receipts:
            assert required_keys.issubset(r.keys())

    def test_flattened_invoices_format(self, repository: P2PRepository) -> None:
        """扁平化发票数据格式正确。"""
        invoices = repository.get_flattened_invoices()
        assert len(invoices) > 0
        required_keys = {
            "invoice_num", "po_number", "invoice_amount",
            "due_date", "discount_due_date",
        }
        for inv in invoices:
            assert required_keys.issubset(inv.keys())

    def test_flattened_payments_format(self, repository: P2PRepository) -> None:
        """扁平化付款数据格式正确。"""
        payments = repository.get_flattened_payments()
        assert len(payments) > 0
        required_keys = {
            "check_number", "invoice_num", "amount", "check_date",
        }
        for p in payments:
            assert required_keys.issubset(p.keys())

    def test_contract_prices(self, repository: P2PRepository) -> None:
        """合同价格映射应包含所有物料。"""
        prices = repository.get_contract_prices()
        assert isinstance(prices, dict)
        assert len(prices) > 0
        assert all(isinstance(v, float) for v in prices.values())


class TestResetAndSeed:
    """清空重建测试。"""

    def test_reset_and_seed_clears_old_data(self, db_engine) -> None:
        """reset_and_seed 应先清空旧数据再重新插入。"""
        from core.database.init_db import reset_and_seed
        from modules.p2p.mock_data.generator import MockDataGenerator
        # 第二次调用：清空 + 重建，记录数应与 count 一致
        counts = reset_and_seed(db_engine, seed=0, count=20, data_generator_factory=MockDataGenerator)
        assert counts["po_headers"] == 20
        assert counts["ap_invoices"] == 20
        assert counts["ap_checks"] == 20

    def test_reset_and_seed_different_count(self, db_engine) -> None:
        """不同 count 应产生不同记录数。"""
        from core.database.init_db import reset_and_seed
        from modules.p2p.mock_data.generator import MockDataGenerator
        reset_and_seed(db_engine, seed=0, count=30, data_generator_factory=MockDataGenerator)
        from sqlalchemy.orm import sessionmaker
        repo = P2PRepository(sessionmaker(bind=db_engine, expire_on_commit=False))
        assert len(repo.query_purchase_orders(days=0)) == 30
