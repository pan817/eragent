"""P2P 模块测试 fixtures。"""

from __future__ import annotations

from typing import Any

import pytest

from modules.p2p.repository import P2PRepository
from modules.p2p.settings import P2PSettings


@pytest.fixture()
def p2p_settings() -> P2PSettings:
    """创建测试用 P2PSettings 实例（不依赖 config.yaml）。"""
    return P2PSettings()


@pytest.fixture()
def repository(db_session_factory) -> P2PRepository:
    """返回基于 SQLite 内存库的 P2PRepository 实例。"""
    return P2PRepository(db_session_factory)


@pytest.fixture()
def mock_po_data() -> list[dict[str, Any]]:
    """返回测试用采购订单数据（5 条，含 1 条异常）。"""
    return [
        {
            "po_number": "PO-001",
            "supplier_id": "SUP-001",
            "supplier_name": "测试供应商A",
            "material_category": "机械零件",
            "po_amount": 100000.00,
            "po_quantity": 500.0,
            "unit_price": 200.0,
            "contract_price": 200.0,
            "required_date": "2026-03-15",
            "material_code": "MAT-001",
            "material_name": "轴承",
            "line_number": "1",
        },
        {
            "po_number": "PO-002",
            "supplier_id": "SUP-001",
            "supplier_name": "测试供应商A",
            "material_category": "机械零件",
            "po_amount": 50000.00,
            "po_quantity": 200.0,
            "unit_price": 250.0,
            "contract_price": 250.0,
            "required_date": "2026-03-20",
            "material_code": "MAT-002",
            "material_name": "齿轮",
            "line_number": "1",
        },
        {
            "po_number": "PO-003",
            "supplier_id": "SUP-002",
            "supplier_name": "测试供应商B",
            "material_category": "电子元器件",
            "po_amount": 200000.00,
            "po_quantity": 1000.0,
            "unit_price": 200.0,
            "contract_price": 200.0,
            "required_date": "2026-03-25",
            "material_code": "MAT-003",
            "material_name": "芯片",
            "line_number": "1",
        },
        {
            "po_number": "PO-004",
            "supplier_id": "SUP-002",
            "supplier_name": "测试供应商B",
            "material_category": "电子元器件",
            "po_amount": 80000.00,
            "po_quantity": 400.0,
            "unit_price": 200.0,
            "contract_price": 195.0,
            "required_date": "2026-03-28",
            "material_code": "MAT-004",
            "material_name": "电容",
            "line_number": "1",
        },
        {
            "po_number": "PO-005",
            "supplier_id": "SUP-003",
            "supplier_name": "测试供应商C",
            "material_category": "原材料",
            "po_amount": 600000.00,
            "po_quantity": 3000.0,
            "unit_price": 200.0,
            "contract_price": 180.0,
            "required_date": "2026-04-01",
            "material_code": "MAT-005",
            "material_name": "钢材",
            "line_number": "1",
        },
    ]


@pytest.fixture()
def mock_gr_data() -> list[dict[str, Any]]:
    """返回测试用收货数据。"""
    return [
        {"po_number": "PO-001", "gr_number": "GR-001", "gr_quantity": 480.0, "receipt_date": "2026-03-14", "quality_passed": True},
        {"po_number": "PO-002", "gr_number": "GR-002", "gr_quantity": 200.0, "receipt_date": "2026-03-19", "quality_passed": True},
        {"po_number": "PO-003", "gr_number": "GR-003", "gr_quantity": 850.0, "receipt_date": "2026-03-24", "quality_passed": False},
        {"po_number": "PO-004", "gr_number": "GR-004", "gr_quantity": 400.0, "receipt_date": "2026-03-27", "quality_passed": True},
        {"po_number": "PO-005", "gr_number": "GR-005", "gr_quantity": 3000.0, "receipt_date": "2026-03-31", "quality_passed": True},
    ]


@pytest.fixture()
def mock_invoice_data() -> list[dict[str, Any]]:
    """返回测试用发票数据（含金额偏差）。"""
    return [
        {"po_number": "PO-001", "invoice_number": "INV-001", "invoice_amount": 112000.00, "supplier_name": "测试供应商A", "due_date": "2026-04-01"},
        {"po_number": "PO-002", "invoice_number": "INV-002", "invoice_amount": 50000.00, "supplier_name": "测试供应商A", "due_date": "2026-04-05"},
        {"po_number": "PO-003", "invoice_number": "INV-003", "invoice_amount": 195000.00, "supplier_name": "测试供应商B", "due_date": "2026-04-10"},
        {"po_number": "PO-004", "invoice_number": "INV-004", "invoice_amount": 80000.00, "supplier_name": "测试供应商B", "due_date": "2026-04-15"},
        {"po_number": "PO-005", "invoice_number": "INV-005", "invoice_amount": 700000.00, "supplier_name": "测试供应商C", "due_date": "2026-04-20"},
    ]


@pytest.fixture()
def mock_payment_data() -> list[dict[str, Any]]:
    """返回测试用付款数据（含逾期和提前付款）。"""
    return [
        {"payment_number": "PAY-001", "invoice_number": "INV-001", "payment_date": "2026-04-11", "payment_amount": 112000.00},
        {"payment_number": "PAY-002", "invoice_number": "INV-002", "payment_date": "2026-04-04", "payment_amount": 50000.00},
        {"payment_number": "PAY-003", "invoice_number": "INV-003", "payment_date": "2026-03-26", "payment_amount": 195000.00},
        {"payment_number": "PAY-004", "invoice_number": "INV-004", "payment_date": "2026-04-20", "payment_amount": 76000.00},
    ]
