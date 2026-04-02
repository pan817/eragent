"""
数据库初始化：建表 + 灌入种子数据。

提供两个独立操作：
- create_tables：仅建表（服务启动时调用）
- reset_and_seed：清空所有表 + 重新灌入种子数据（HTTP 接口触发）
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from core.database.models import (
    ApInvoice,
    ApPayment,
    ApSupplier,
    Base,
    PoHeader,
    PoLine,
    PoLineLocation,
    RcvTransaction,
)
from modules.p2p.mock_data.generator import MockDataGenerator


def _parse_date(date_str: str) -> date:
    """将 'YYYY-MM-DD' 字符串转为 date 对象。"""
    return date.fromisoformat(date_str)


# 按外键依赖顺序排列（先删子表，再删父表）
_TABLES_DELETE_ORDER = [
    ApPayment,
    ApInvoice,
    RcvTransaction,
    PoLineLocation,
    PoLine,
    PoHeader,
    ApSupplier,
]


def _truncate_all(session: Session) -> None:
    """按外键依赖顺序清空所有业务表。"""
    for model in _TABLES_DELETE_ORDER:
        session.execute(delete(model))
    session.commit()


def _insert_data(session: Session, seed: int, count: int) -> None:
    """使用 MockDataGenerator 生成数据并批量插入。"""
    gen = MockDataGenerator(seed=seed)
    raw = gen.generate_all(count=count)

    # 供应商
    for s in raw["suppliers"]:
        session.add(ApSupplier(
            supplier_id=s["supplier_id"],
            supplier_name=s["supplier_name"],
            supplier_site_id=s["supplier_site_id"],
            payment_terms=s["payment_terms"],
            status=s["status"],
        ))

    # PO 头
    for h in raw["po_headers"]:
        session.add(PoHeader(
            po_header_id=h["po_header_id"],
            po_number=h["po_number"],
            supplier_id=h["supplier_id"],
            supplier_name=h["supplier_name"],
            status=h["status"],
            creation_date=_parse_date(h["creation_date"]),
            total_amount=h["total_amount"],
            currency=h["currency"],
        ))

    # PO 行
    for l in raw["po_lines"]:
        session.add(PoLine(
            po_line_id=l["po_line_id"],
            po_header_id=l["po_header_id"],
            po_number=l["po_number"],
            line_num=l["line_num"],
            item_code=l["item_code"],
            item_description=l["item_description"],
            quantity=l["quantity"],
            unit_price=l["unit_price"],
            amount=l["amount"],
            category=l["category"],
            standard_price=l["standard_price"],
        ))

    # PO 行位置
    for loc in raw["po_line_locations"]:
        session.add(PoLineLocation(
            line_location_id=loc["line_location_id"],
            po_line_id=loc["po_line_id"],
            po_number=loc["po_number"],
            promised_date=_parse_date(loc["promised_date"]),
            need_by_date=_parse_date(loc["need_by_date"]),
            quantity=loc["quantity"],
        ))

    # 收货事务
    for t in raw["rcv_transactions"]:
        session.add(RcvTransaction(
            transaction_id=t["transaction_id"],
            shipment_header_id=t["shipment_header_id"],
            po_number=t["po_number"],
            po_line_id=t["po_line_id"],
            transaction_type=t["transaction_type"],
            quantity=t["quantity"],
            accepted_quantity=t["accepted_quantity"],
            rejected_quantity=t["rejected_quantity"],
            transaction_date=_parse_date(t["transaction_date"]),
            supplier_id=t["supplier_id"],
        ))

    # 发票
    for inv in raw["invoices"]:
        session.add(ApInvoice(
            invoice_id=inv["invoice_id"],
            invoice_number=inv["invoice_number"],
            po_number=inv["po_number"],
            supplier_id=inv["supplier_id"],
            supplier_name=inv["supplier_name"],
            invoice_amount=inv["invoice_amount"],
            invoice_date=_parse_date(inv["invoice_date"]),
            due_date=_parse_date(inv["due_date"]),
            discount_due_date=_parse_date(inv["discount_due_date"]) if inv.get("discount_due_date") else None,
            status=inv["status"],
            payment_terms=inv.get("payment_terms", "NET30"),
        ))

    # 付款
    for p in raw["payments"]:
        session.add(ApPayment(
            payment_id=p["payment_id"],
            payment_number=p["payment_number"],
            invoice_number=p["invoice_number"],
            supplier_id=p["supplier_id"],
            payment_amount=p["payment_amount"],
            payment_date=_parse_date(p["payment_date"]),
            payment_method=p["payment_method"],
        ))

    session.commit()


def create_tables(engine: Engine) -> None:
    """仅建表（不灌数据），服务启动时调用。

    Args:
        engine: SQLAlchemy Engine。
    """
    Base.metadata.create_all(engine)


def reset_and_seed(engine: Engine, seed: int = 42, count: int = 500) -> dict[str, int]:
    """清空所有业务表并重新灌入种子数据。

    Args:
        engine: SQLAlchemy Engine。
        seed: 随机种子，确保数据可重复。
        count: 生成的采购订单数量（发票、付款等同步生成相同数量）。

    Returns:
        各表插入的记录数。
    """
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        _truncate_all(session)
        _insert_data(session, seed=seed, count=count)

        # 统计各表记录数
        counts = {}
        for model in _TABLES_DELETE_ORDER:
            n = session.scalar(select(func.count()).select_from(model))
            counts[model.__tablename__] = n or 0
        return counts


def init_database(engine: Engine, seed: int = 42, count: int = 50) -> None:
    """建表 + 灌入种子数据（向后兼容，测试用）。

    Args:
        engine: SQLAlchemy Engine。
        seed: 随机种子。
        count: 生成的记录数。
    """
    create_tables(engine)
    reset_and_seed(engine, seed=seed, count=count)
