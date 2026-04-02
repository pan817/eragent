"""
共享测试 fixtures。

提供 Settings、P2PSettings、模拟业务数据等可复用 fixture，
确保所有测试不依赖外部服务（Neo4j、PostgreSQL、Chroma、LLM API）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# 确保项目根目录在 sys.path 中，以便 `from config.settings import ...` 等导入正常工作
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import (  # noqa: E402
    P2PSettings,
    Settings,
)
from core.database import (  # noqa: E402
    Base,
    P2PRepository,
    get_session_factory,
    init_database,
)
from core.database.engine import create_engine_from_dsn  # noqa: E402


# ============================================================
# 配置 fixtures
# ============================================================


@pytest.fixture()
def settings() -> Settings:
    """创建测试用 Settings 实例（不依赖 config.yaml）。"""
    return Settings(
        app_name="ERP Agent Test",
        app_version="0.1.0-test",
        debug=True,
        language="zh",
    )


@pytest.fixture()
def p2p_settings(settings: Settings) -> P2PSettings:
    """提取 P2PSettings 子配置。"""
    return settings.p2p


# ============================================================
# 数据库 fixtures（SQLite 内存库）
# ============================================================


@pytest.fixture()
def db_engine():
    """创建 SQLite 内存数据库引擎，灌入种子数据。"""
    engine = create_engine_from_dsn("sqlite:///:memory:")
    init_database(engine, seed=0)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session_factory(db_engine):
    """返回绑定到测试引擎的 Session 工厂。"""
    return get_session_factory(db_engine)


@pytest.fixture()
def repository(db_session_factory) -> P2PRepository:
    """返回基于 SQLite 内存库的 P2PRepository 实例。"""
    return P2PRepository(db_session_factory)


# ============================================================
# 模拟业务数据 fixtures
# ============================================================


@pytest.fixture()
def mock_po_data() -> list[dict[str, Any]]:
    """返回测试用采购订单数据（5 条，含 1 条异常）。

    PO-005 的 po_amount 为 600000（超过高金额阈值 500000），
    用于触发 HIGH 严重等级。
    """
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
    """返回测试用收货数据。

    GR-001 数量 480（vs PO-001 的 500），偏差 4%，在默认 5% 容差边界附近。
    GR-003 数量 850（vs PO-003 的 1000），偏差 15%，明显超容差。
    """
    return [
        {
            "po_number": "PO-001",
            "gr_number": "GR-001",
            "gr_quantity": 480.0,
            "receipt_date": "2026-03-14",
            "quality_passed": True,
        },
        {
            "po_number": "PO-002",
            "gr_number": "GR-002",
            "gr_quantity": 200.0,
            "receipt_date": "2026-03-19",
            "quality_passed": True,
        },
        {
            "po_number": "PO-003",
            "gr_number": "GR-003",
            "gr_quantity": 850.0,
            "receipt_date": "2026-03-24",
            "quality_passed": False,
        },
        {
            "po_number": "PO-004",
            "gr_number": "GR-004",
            "gr_quantity": 400.0,
            "receipt_date": "2026-03-27",
            "quality_passed": True,
        },
        {
            "po_number": "PO-005",
            "gr_number": "GR-005",
            "gr_quantity": 3000.0,
            "receipt_date": "2026-03-31",
            "quality_passed": True,
        },
    ]


@pytest.fixture()
def mock_invoice_data() -> list[dict[str, Any]]:
    """返回测试用发票数据（含金额偏差）。

    INV-001 金额 112000（vs PO-001 的 100000），偏差 12%，超容差。
    INV-003 金额 195000（vs PO-003 的 200000），偏差 2.5%，在容差内。
    INV-005 金额 700000（vs PO-005 的 600000），偏差 16.67%，高金额+高偏差。
    """
    return [
        {
            "po_number": "PO-001",
            "invoice_number": "INV-001",
            "invoice_amount": 112000.00,
            "supplier_name": "测试供应商A",
            "due_date": "2026-04-01",
        },
        {
            "po_number": "PO-002",
            "invoice_number": "INV-002",
            "invoice_amount": 50000.00,
            "supplier_name": "测试供应商A",
            "due_date": "2026-04-05",
        },
        {
            "po_number": "PO-003",
            "invoice_number": "INV-003",
            "invoice_amount": 195000.00,
            "supplier_name": "测试供应商B",
            "due_date": "2026-04-10",
        },
        {
            "po_number": "PO-004",
            "invoice_number": "INV-004",
            "invoice_amount": 80000.00,
            "supplier_name": "测试供应商B",
            "due_date": "2026-04-15",
        },
        {
            "po_number": "PO-005",
            "invoice_number": "INV-005",
            "invoice_amount": 700000.00,
            "supplier_name": "测试供应商C",
            "due_date": "2026-04-20",
        },
    ]


@pytest.fixture()
def mock_payment_data() -> list[dict[str, Any]]:
    """返回测试用付款数据（含逾期和提前付款）。

    PAY-001：逾期 10 天。
    PAY-002：正常付款。
    PAY-003：提前 15 天。
    PAY-004：逾期后的折扣滥用（超过折扣截止日仍按折扣金额付款）。
    """
    return [
        {
            "payment_number": "PAY-001",
            "invoice_number": "INV-001",
            "payment_date": "2026-04-11",
            "payment_amount": 112000.00,
        },
        {
            "payment_number": "PAY-002",
            "invoice_number": "INV-002",
            "payment_date": "2026-04-04",
            "payment_amount": 50000.00,
        },
        {
            "payment_number": "PAY-003",
            "invoice_number": "INV-003",
            "payment_date": "2026-03-26",
            "payment_amount": 195000.00,
        },
        {
            "payment_number": "PAY-004",
            "invoice_number": "INV-004",
            "payment_date": "2026-04-20",
            "payment_amount": 76000.00,
        },
    ]
