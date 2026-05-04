"""
数据库初始化：建表 + 灌入种子数据。

提供两个独立操作：
- create_tables：仅建表（服务启动时调用）
- reset_and_seed：清空所有表 + 重新灌入种子数据（HTTP 接口触发）
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from core.database.models import (
    ApInvoice,
    ApInvoiceDistribution,
    ApInvoiceLine,
    ApInvoicePayment,
    ApPayment,
    ApPaymentSchedule,
    ApSupplier,
    ApSupplierSite,
    Base,
    MtlSystemItem,
    OkcKHeader,
    OkcKLine,
    PoDistribution,
    PoHeader,
    PoLine,
    PoLineLocation,
    PonAuctionHeader,
    PonBidHeader,
    RcvShipmentHeader,
    RcvShipmentLine,
    RcvTransaction,
)
from typing import Any, Callable


def _parse_date(date_str: str) -> date:
    """将 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS' 字符串转为 date 对象。"""
    return date.fromisoformat(date_str[:10])


def _parse_datetime(date_str: str) -> datetime:
    """将 'YYYY-MM-DD' 字符串转为 datetime 对象。"""
    return datetime.fromisoformat(date_str)


def _parse_datetime_opt(val: str | None) -> datetime | None:
    """将可选的 'YYYY-MM-DD' 字符串转为 datetime 对象或 None。"""
    if val is None:
        return None
    return datetime.fromisoformat(val)


def _parse_date_opt(val: str | None) -> date | None:
    """将可选的 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS' 字符串转为 date 对象或 None。"""
    if val is None:
        return None
    return date.fromisoformat(val[:10])


# 按外键依赖顺序排列（先删子表，再删父表）
_TABLES_DELETE_ORDER = [
    OkcKLine,
    OkcKHeader,
    PonBidHeader,
    PonAuctionHeader,
    ApPaymentSchedule,
    ApInvoicePayment,
    ApInvoiceDistribution,
    ApInvoiceLine,
    ApPayment,
    ApInvoice,
    RcvTransaction,
    RcvShipmentLine,
    RcvShipmentHeader,
    PoDistribution,
    PoLineLocation,
    PoLine,
    PoHeader,
    MtlSystemItem,
    ApSupplierSite,
    ApSupplier,
]


def _truncate_all(session: Session) -> None:
    """按外键依赖顺序清空所有业务表。"""
    for model in _TABLES_DELETE_ORDER:
        session.execute(delete(model))
    session.commit()


def _insert_data(session: Session, raw: dict[str, list]) -> None:
    """将预生成的数据批量插入数据库。"""

    # 供应商
    for s in raw["suppliers"]:
        session.add(ApSupplier(
            vendor_id=s["vendor_id"],
            vendor_name=s["vendor_name"],
            supplier_site_id=s["supplier_site_id"],
            terms_id=s["terms_id"],
            enabled_flag=s["enabled_flag"],
            segment1=s.get("segment1"),
            vendor_type_lookup_code=s.get("vendor_type_lookup_code"),
            start_date_active=_parse_date_opt(s.get("start_date_active")),
            standard_industry_class=s.get("standard_industry_class"),
            last_update_date=_parse_datetime_opt(s.get("last_update_date")),
        ))

    # 供应商地点
    for site in raw["supplier_sites"]:
        session.add(ApSupplierSite(
            vendor_site_id=site["vendor_site_id"],
            vendor_id=site["vendor_id"],
            vendor_site_code=site.get("vendor_site_code"),
            address_line1=site.get("address_line1"),
            city=site.get("city"),
            country=site.get("country"),
            phone=site.get("phone"),
            email_address=site.get("email_address"),
            purchasing_site_flag=site.get("purchasing_site_flag"),
            pay_site_flag=site.get("pay_site_flag"),
            org_id=site.get("org_id"),
            last_update_date=_parse_datetime_opt(site.get("last_update_date")),
        ))

    # 物料主数据
    for m in raw["materials"]:
        session.add(MtlSystemItem(
            inventory_item_id=m["inventory_item_id"],
            organization_id=m["organization_id"],
            segment1=m.get("segment1"),
            description=m.get("description"),
            primary_uom_code=m.get("primary_uom_code"),
            item_type=m.get("item_type"),
            list_price_per_unit=m.get("list_price_per_unit"),
            purchasing_item_flag=m.get("purchasing_item_flag"),
            purchasing_enabled_flag=m.get("purchasing_enabled_flag"),
            inventory_item_status_code=m.get("inventory_item_status_code"),
        ))

    # PO 头
    for h in raw["po_headers"]:
        session.add(PoHeader(
            po_header_id=h["po_header_id"],
            po_number=h["po_number"],
            vendor_id=h["vendor_id"],
            vendor_name=h["vendor_name"],
            status=h["status"],
            creation_date=_parse_datetime(h["creation_date"]),
            total_amount=h["total_amount"],
            currency=h["currency"],
            type_lookup_code=h.get("type_lookup_code"),
            authorization_status=h.get("authorization_status"),
            buyer_id=h.get("buyer_id"),
            org_id=h.get("org_id"),
            last_update_date=_parse_datetime_opt(h.get("last_update_date")),
        ))

    # PO 行
    for ln in raw["po_lines"]:
        session.add(PoLine(
            po_line_id=ln["po_line_id"],
            po_header_id=ln["po_header_id"],
            po_number=ln["po_number"],
            line_num=ln["line_num"],
            item_id=ln["item_id"],
            item_description=ln["item_description"],
            quantity=ln["quantity"],
            unit_price=ln["unit_price"],
            amount=ln["amount"],
            category_id=ln["category_id"],
            standard_price=ln["standard_price"],
            unit_meas_lookup_code=ln.get("unit_meas_lookup_code"),
            last_update_date=_parse_datetime_opt(ln.get("last_update_date")),
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
            last_update_date=_parse_datetime_opt(loc.get("last_update_date")),
        ))

    # PO 分配
    for dist in raw["po_distributions"]:
        session.add(PoDistribution(
            po_distribution_id=dist["po_distribution_id"],
            po_header_id=dist["po_header_id"],
            po_line_id=dist["po_line_id"],
            line_location_id=dist["line_location_id"],
            quantity_ordered=dist.get("quantity_ordered"),
            destination_type_code=dist.get("destination_type_code"),
            org_id=dist.get("org_id"),
        ))

    # 收货单头
    for rh in raw["rcv_headers"]:
        session.add(RcvShipmentHeader(
            shipment_header_id=rh["shipment_header_id"],
            receipt_num=rh.get("receipt_num"),
            shipped_date=_parse_date_opt(rh.get("shipped_date")),
            expected_receipt_date=_parse_date_opt(rh.get("expected_receipt_date")),
            last_update_date=_parse_datetime_opt(rh.get("last_update_date")),
        ))

    # 收货单行
    for sl in raw["shipment_lines"]:
        session.add(RcvShipmentLine(
            shipment_line_id=sl["shipment_line_id"],
            shipment_header_id=sl["shipment_header_id"],
            line_num=sl.get("line_num"),
            po_header_id=sl.get("po_header_id"),
            po_line_id=sl.get("po_line_id"),
            item_id=sl.get("item_id"),
            quantity_shipped=sl.get("quantity_shipped"),
            quantity_received=sl.get("quantity_received"),
            unit_of_measure=sl.get("unit_of_measure"),
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
            transaction_date=_parse_datetime(t["transaction_date"]),
            vendor_id=t["vendor_id"],
            source_document_code=t.get("source_document_code"),
            last_update_date=_parse_datetime_opt(t.get("last_update_date")),
        ))

    # 发票
    for inv in raw["invoices"]:
        session.add(ApInvoice(
            invoice_id=inv["invoice_id"],
            invoice_num=inv["invoice_num"],
            po_number=inv["po_number"],
            vendor_id=inv["vendor_id"],
            vendor_name=inv["vendor_name"],
            invoice_amount=inv["invoice_amount"],
            invoice_date=_parse_datetime(inv["invoice_date"]),
            due_date=_parse_date(inv["due_date"]),
            discount_due_date=_parse_date_opt(inv.get("discount_due_date")),
            approval_status=inv["approval_status"],
            terms_id=inv.get("terms_id", "NET30"),
            invoice_type_lookup_code=inv.get("invoice_type_lookup_code"),
            invoice_currency_code=inv.get("invoice_currency_code"),
            last_update_date=_parse_datetime_opt(inv.get("last_update_date")),
        ))

    # 发票行
    for il in raw["invoice_lines"]:
        session.add(ApInvoiceLine(
            invoice_line_id=il["invoice_line_id"],
            invoice_id=il["invoice_id"],
            line_number=il.get("line_number"),
            line_type_lookup_code=il.get("line_type_lookup_code"),
            amount=il.get("amount"),
            quantity_invoiced=il.get("quantity"),
            accounting_date=_parse_date_opt(il.get("accounting_date")),
            last_update_date=_parse_datetime_opt(il.get("last_update_date")),
        ))

    # 发票分配
    for idist in raw["invoice_distributions"]:
        session.add(ApInvoiceDistribution(
            invoice_distribution_id=idist["invoice_distribution_id"],
            invoice_id=idist["invoice_id"],
            invoice_line_number=idist.get("invoice_line_number"),
            distribution_line_number=idist.get("distribution_line_number"),
            amount=idist.get("amount"),
            accounting_date=_parse_date_opt(idist.get("accounting_date")),
            match_status_flag=idist.get("match_status_flag"),
            posted_flag=idist.get("posted_flag"),
        ))

    # 付款
    for p in raw["payments"]:
        session.add(ApPayment(
            check_id=p["check_id"],
            check_number=p["check_number"],
            invoice_num=p["invoice_num"],
            vendor_id=p["vendor_id"],
            amount=p["amount"],
            check_date=_parse_datetime(p["check_date"]),
            payment_method_code=p["payment_method_code"],
            status_lookup_code=p.get("status_lookup_code"),
            currency_code=p.get("currency_code"),
            last_update_date=_parse_datetime_opt(p.get("last_update_date")),
        ))

    # 发票付款关联
    for ip in raw["invoice_payments"]:
        session.add(ApInvoicePayment(
            invoice_payment_id=ip["invoice_payment_id"],
            invoice_id=ip["invoice_id"],
            check_id=ip["check_id"],
            payment_num=ip.get("payment_num"),
            amount=ip.get("amount"),
            accounting_date=_parse_date_opt(ip.get("accounting_date")),
        ))

    # 付款计划
    for ps in raw["payment_schedules"]:
        session.add(ApPaymentSchedule(
            payment_schedule_id=ps["payment_schedule_id"],
            invoice_id=ps["invoice_id"],
            payment_num=ps.get("payment_num"),
            due_date=_parse_date_opt(ps.get("due_date")),
            discount_date=_parse_date_opt(ps.get("discount_date")),
            gross_amount=ps.get("gross_amount"),
            amount_remaining=ps.get("amount_remaining"),
            payment_status_flag=ps.get("payment_status_flag"),
        ))

    # 寻源事件
    for auc in raw["auctions"]:
        session.add(PonAuctionHeader(
            auction_header_id=auc["auction_header_id"],
            document_number=auc.get("document_number"),
            auction_title=auc.get("auction_title"),
            auction_type=auc.get("auction_type"),
            auction_status=auc.get("auction_status"),
            open_bidding_date=_parse_datetime_opt(auc.get("open_bidding_date")),
            close_bidding_date=_parse_datetime_opt(auc.get("close_bidding_date")),
            outcome=auc.get("outcome"),
            org_id=auc.get("org_id"),
        ))

    # 投标
    for bid in raw["bids"]:
        session.add(PonBidHeader(
            bid_number=bid["bid_number"],
            auction_header_id=bid["auction_header_id"],
            bid_status=bid.get("bid_status"),
            vendor_id=bid.get("vendor_id"),
            bid_total=bid.get("bid_total"),
            bid_currency_code=bid.get("bid_currency_code"),
            publish_date=_parse_datetime_opt(bid.get("publish_date")),
            award_status=bid.get("award_status"),
            award_date=_parse_date_opt(bid.get("award_date")),
        ))

    # 合同头
    for cnt in raw["contracts"]:
        session.add(OkcKHeader(
            id=cnt["id"],
            contract_number=cnt.get("contract_number"),
            sts_code=cnt.get("sts_code"),
            start_date=_parse_date_opt(cnt.get("start_date")),
            end_date=_parse_date_opt(cnt.get("end_date")),
            estimated_amount=cnt.get("estimated_amount"),
            currency_code=cnt.get("currency_code"),
            authoring_org_id=cnt.get("authoring_org_id"),
            buy_or_sell=cnt.get("buy_or_sell"),
            description=cnt.get("description"),
        ))

    # 合同行
    for cl in raw["contract_lines"]:
        session.add(OkcKLine(
            id=cl["id"],
            chr_id=cl["chr_id"],
            line_number=cl.get("line_number"),
            sts_code=cl.get("sts_code"),
            start_date=_parse_date_opt(cl.get("start_date")),
            end_date=_parse_date_opt(cl.get("end_date")),
            item_id=cl.get("item_id"),
            item_description=cl.get("item_description"),
            price_unit=cl.get("price_unit"),
            price_negotiated=cl.get("price_negotiated"),
            quantity=cl.get("quantity"),
            uom_code=cl.get("uom_code"),
        ))

    session.commit()


def create_tables(engine: Engine) -> None:
    """建表入口，服务启动时调用。

    PostgreSQL：通过 ``alembic upgrade head`` 执行迁移（幂等），由 alembic
    版本追踪保证不会重复创建已有表/类型。
    SQLite：直接使用 ``metadata.create_all``（测试场景，无 alembic 迁移历史）。

    Args:
        engine: SQLAlchemy Engine。
    """
    if engine.dialect.name == "sqlite":
        # 测试用 SQLite 内存库，直接 create_all
        from core.memory.tables import metadata_obj
        import core.chat.tables  # noqa: F401
        import core.etl.tables  # noqa: F401
        import core.orchestrator.dag.tables  # noqa: F401

        Base.metadata.create_all(engine)
        metadata_obj.create_all(engine)
        return

    # PostgreSQL：走 alembic 迁移（advisory lock 在 env.py 中实现）
    import logging
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, text

    logger = logging.getLogger(__name__)

    project_root = Path(__file__).resolve().parent.parent.parent
    alembic_ini = project_root / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini))
    alembic_cfg.set_main_option(
        "script_location", str(project_root / "migrations")
    )
    alembic_cfg.set_main_option("sqlalchemy.url", str(engine.url))

    # 兼容存量数据库：表已由之前的 create_all 创建，但 alembic_version 未标记。
    # 直接 upgrade 会导致 baseline 迁移尝试重建已有表/类型而失败。
    # 检测到此情况后先 stamp head，跳过所有历史迁移。
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    has_business_tables = "ap_suppliers" in existing_tables
    has_alembic_version = "alembic_version" in existing_tables

    if has_business_tables and not has_alembic_version:
        logger.info(
            "Existing database detected without alembic_version — "
            "stamping current state as head."
        )
        command.stamp(alembic_cfg, "head")
    elif has_business_tables and has_alembic_version:
        # alembic_version 表存在但可能为空（上次迁移失败回滚）
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            if row is None:
                logger.info(
                    "alembic_version table is empty — "
                    "stamping current state as head."
                )
                command.stamp(alembic_cfg, "head")

    logger.info("Running alembic upgrade head ...")
    command.upgrade(alembic_cfg, "head")
    logger.info("Alembic migrations applied successfully.")


def reset_and_seed(
    engine: Engine,
    seed: int = 42,
    count: int = 500,
    data_generator_factory: Callable[..., Any] | None = None,
) -> dict[str, int]:
    """清空所有业务表并重新灌入种子数据。

    Args:
        engine: SQLAlchemy Engine。
        seed: 随机种子，确保数据可重复。
        count: 生成的采购订单数量（发票、付款等同步生成相同数量）。
        data_generator_factory: 数据生成器类（需接受 seed 参数，返回的
            实例须提供 ``generate_all(count=int)`` 方法）。
            由 API 层传入，解耦 core 对具体业务模块的依赖。

    Returns:
        各表插入的记录数。
    """
    if data_generator_factory is None:
        raise ValueError(
            "data_generator_factory is required — pass the module-specific "
            "generator class (e.g. MockDataGenerator) from the API / test layer"
        )
    gen = data_generator_factory(seed=seed)
    raw = gen.generate_all(count=count)

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        _truncate_all(session)
        _insert_data(session, raw)

        # 统计各表记录数
        counts = {}
        for model in _TABLES_DELETE_ORDER:
            n = session.scalar(select(func.count()).select_from(model))
            counts[model.__tablename__] = n or 0
        return counts


def init_database(
    engine: Engine,
    seed: int = 42,
    count: int = 500,
    data_generator_factory: Callable[..., Any] | None = None,
) -> None:
    """建表 + 灌入种子数据（向后兼容，测试用）。

    Args:
        engine: SQLAlchemy Engine。
        seed: 随机种子。
        count: 生成的记录数。
        data_generator_factory: 数据生成器类。
    """
    create_tables(engine)
    reset_and_seed(engine, seed=seed, count=count, data_generator_factory=data_generator_factory)
