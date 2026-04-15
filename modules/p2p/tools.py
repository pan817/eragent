"""
P2P Agent 工具集。

使用 LangChain @tool 装饰器定义结构化工具，供 P2P Agent 调用。
每个工具接受结构化参数，返回 JSON 字符串结果。

工具分为两类：
- query_* 工具：查询采购订单、收货、发票、付款等业务数据（通过 PostgreSQL 查询）
- run_* / calculate_* 工具：执行规则检查和 KPI 计算
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain.tools import tool

from config.settings import get_settings
from core.database.repository import P2PRepository
from modules.p2p.rules import (
    PaymentComplianceChecker,
    PriceVarianceAnalyzer,
    SupplierPerformanceCalculator,
    ThreeWayMatchChecker,
)


# ============================================================
# Repository 访问（模块级变量，由外部注入）
# ============================================================

_repository: P2PRepository | None = None


def set_repository(repo: P2PRepository) -> None:
    """注入 P2PRepository 实例（服务启动时调用）。"""
    global _repository
    _repository = repo


def _get_repository() -> P2PRepository:
    """获取已注入的 Repository 实例。"""
    if _repository is None:
        raise RuntimeError(
            "P2PRepository 未初始化。请确保在服务启动时调用 set_repository()。"
        )
    return _repository


def _clip_and_dump(obj: Any) -> str:
    """把 tool 返回的 Python 对象裁剪后序列化为 JSON 字符串。

    两级预算（读取 ``settings.p2p.tool_output``）：

    1. ``max_items``：列表型结构（顶层 list 或 dict 中的 list 字段）最多保留
       前 N 条，尾部追加一条 ``{"_truncated": true, "dropped": M, "reason": ...}``
       摘要记录，向 LLM 显式暴露"还有 M 条同类数据未列出"。
    2. ``max_chars``：序列化后的 JSON 总字符数兜底；超出时继续按顺序丢弃列表
       尾部元素直到达标（若无列表可裁，最后退化到按字符硬切）。

    注意：只在结构层裁剪，保证截断后仍是合法 JSON；LLM 能正确解析并理解"数据
    已被系统预算裁剪"，而不是解析到半截坏 JSON。
    """
    from config.settings import get_settings

    cfg = get_settings().p2p.tool_output
    max_items = cfg.max_items
    max_chars = cfg.max_chars

    def _clip_list(items: list[Any]) -> list[Any]:
        if max_items <= 0 or len(items) <= max_items:
            return items
        dropped = len(items) - max_items
        return [
            *items[:max_items],
            {
                "_truncated": True,
                "dropped": dropped,
                "reason": (
                    f"列表总长 {len(items)} 条，按 p2p.tool_output.max_items="
                    f"{max_items} 裁剪，仅保留前 {max_items} 条；剩余 {dropped} "
                    f"条为同类数据，如需完整数据请导出或缩小查询范围。"
                ),
            },
        ]

    if isinstance(obj, list):
        clipped: Any = _clip_list(obj)
    elif isinstance(obj, dict):
        clipped = {
            k: (_clip_list(v) if isinstance(v, list) else v) for k, v in obj.items()
        }
    else:
        clipped = obj

    text = json.dumps(clipped, ensure_ascii=False, indent=2, default=str)

    if max_chars <= 0 or len(text) <= max_chars:
        return text

    # 兜底：按 max_chars 继续丢弃列表尾部元素。优先处理顶层 list；
    # 若是 dict，则找出最长的 list 字段逐步丢弃。
    def _shrink_once(o: Any) -> tuple[Any, bool]:
        if isinstance(o, list) and len(o) > 1:
            return o[:-1], True
        if isinstance(o, dict):
            longest_key: str | None = None
            longest_len = -1
            for k, v in o.items():
                if isinstance(v, list) and len(v) > longest_len:
                    longest_key, longest_len = k, len(v)
            if longest_key is not None and longest_len > 1:
                o = dict(o)
                o[longest_key] = o[longest_key][:-1]
                return o, True
        return o, False

    current = clipped
    while len(text) > max_chars:
        current, shrunk = _shrink_once(current)
        if not shrunk:
            # 无法再结构化裁剪：硬切并追加显式提示（仍是合法 JSON 字符串字段）
            return json.dumps(
                {
                    "_truncated": True,
                    "reason": (
                        f"返回 JSON 超过 p2p.tool_output.max_chars={max_chars} "
                        f"且无可裁剪列表；已丢弃尾部内容。"
                    ),
                    "preview": text[: max(0, max_chars - 200)],
                },
                ensure_ascii=False,
                indent=2,
            )
        text = json.dumps(current, ensure_ascii=False, indent=2, default=str)

    return text


# ============================================================
# 查询工具
# ============================================================


@tool
async def query_purchase_orders(
    supplier_id: str = "",
    status: str = "",
    po_number: str = "",
    days: int = 30,
) -> str:
    """查询采购订单数据。

    根据供应商 ID、订单状态、采购订单号等条件筛选采购订单列表。

    Args:
        supplier_id: 供应商 ID，为空则返回全部。
        status: 订单状态过滤，如 approved、pending，为空则不过滤。
        po_number: 采购订单号，为空则不按 PO 过滤。
        days: 查询最近 N 天内的订单，默认 30 天。

    Returns:
        JSON 格式的采购订单列表字符串。
    """
    repo = _get_repository()
    pos = await asyncio.to_thread(
        repo.query_purchase_orders,
        supplier_id=supplier_id,
        status=status,
        po_number=po_number,
        days=days,
    )
    return _clip_and_dump(pos)


@tool
async def query_receipts(
    po_number: str = "",
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """查询收货记录。

    根据采购订单号或供应商 ID 筛选收货记录。

    Args:
        po_number: 采购订单号，为空则不按 PO 过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        days: 查询最近 N 天内的记录，默认 30 天。

    Returns:
        JSON 格式的收货记录列表字符串。
    """
    repo = _get_repository()
    receipts = await asyncio.to_thread(
        repo.query_receipts,
        po_number=po_number,
        supplier_id=supplier_id,
        days=days,
    )
    return _clip_and_dump(receipts)


@tool
async def query_invoices(
    po_number: str = "",
    supplier_id: str = "",
    status: str = "",
    invoice_number: str = "",
    days: int = 30,
) -> str:
    """查询发票数据。

    根据采购订单号、供应商 ID、发票号或发票状态筛选发票记录。

    Args:
        po_number: 采购订单号，为空则不按 PO 过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        status: 发票状态过滤，如 pending、paid，为空则不过滤。
        invoice_number: 发票号，为空则不按发票过滤。
        days: 查询最近 N 天内的发票，默认 30 天。

    Returns:
        JSON 格式的发票列表字符串。
    """
    repo = _get_repository()
    invoices = await asyncio.to_thread(
        repo.query_invoices,
        po_number=po_number,
        supplier_id=supplier_id,
        status=status,
        invoice_number=invoice_number,
        days=days,
    )
    return _clip_and_dump(invoices)


@tool
async def query_payments(
    invoice_number: str = "",
    supplier_id: str = "",
    payment_number: str = "",
    days: int = 30,
) -> str:
    """查询付款记录。

    根据发票号、供应商 ID 或付款单号筛选付款记录。

    Args:
        invoice_number: 发票号，为空则不按发票过滤。
        supplier_id: 供应商 ID，为空则不按供应商过滤。
        payment_number: 付款单号，为空则不按付款单过滤。
        days: 查询最近 N 天内的付款，默认 30 天。

    Returns:
        JSON 格式的付款记录列表字符串。
    """
    repo = _get_repository()
    payments = await asyncio.to_thread(
        repo.query_payments,
        invoice_number=invoice_number,
        supplier_id=supplier_id,
        payment_number=payment_number,
        days=days,
    )
    return _clip_and_dump(payments)


# ============================================================
# 分析工具
# ============================================================


def _run_three_way_match_sync(po_number: str) -> str:
    """同步执行三路匹配的真实实现，供 async tool 通过线程池调用。"""
    settings = get_settings()
    repo = _get_repository()

    po_lines = repo.get_flattened_purchase_orders(po_number=po_number)
    gr_lines = repo.get_flattened_receipts(po_number=po_number)
    invoice_lines = repo.get_flattened_invoices(po_number=po_number)

    checker = ThreeWayMatchChecker(settings.p2p)
    anomalies = checker.check(po_lines, gr_lines, invoice_lines)

    result: list[dict[str, Any]] = [
        anomaly.model_dump(mode="json") for anomaly in anomalies
    ]
    return _clip_and_dump(result)


@tool
async def run_three_way_match(po_number: str = "") -> str:
    """执行三路匹配检查，比对采购订单、收货单、发票的金额和数量。

    当偏差超过配置容差时返回异常记录。若指定 po_number 则只检查该订单，
    否则检查所有订单。

    Args:
        po_number: 采购订单号，为空则检查所有订单。

    Returns:
        JSON 格式的异常列表字符串，每条包含异常类型、严重等级、偏差详情。
    """
    return await asyncio.to_thread(_run_three_way_match_sync, po_number)


def _run_price_variance_analysis_sync(supplier_id: str, days: int) -> str:
    """同步执行价格差异分析的真实实现。"""
    settings = get_settings()
    repo = _get_repository()

    po_lines = repo.get_flattened_purchase_orders(supplier_id=supplier_id)
    contract_prices = repo.get_contract_prices()

    analyzer = PriceVarianceAnalyzer(settings.p2p)
    anomalies = analyzer.analyze(po_lines, contract_prices)

    result: list[dict[str, Any]] = [
        anomaly.model_dump(mode="json") for anomaly in anomalies
    ]
    return _clip_and_dump(result)


@tool
async def run_price_variance_analysis(
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """执行价格差异分析，比对实际采购单价与合同价/标准价。

    偏差超出容差阈值时返回异常记录。

    Args:
        supplier_id: 供应商 ID，为空则分析所有供应商。
        days: 分析最近 N 天内的采购订单，默认 30 天。

    Returns:
        JSON 格式的价格差异异常列表字符串。
    """
    return await asyncio.to_thread(
        _run_price_variance_analysis_sync, supplier_id, days
    )


def _run_payment_compliance_check_sync(supplier_id: str, days: int) -> str:
    """同步执行付款合规检查的真实实现。"""
    settings = get_settings()
    repo = _get_repository()

    payments = repo.get_flattened_payments(supplier_id=supplier_id)
    invoices = repo.get_flattened_invoices(supplier_id=supplier_id)

    checker = PaymentComplianceChecker(settings.p2p)
    anomalies = checker.check(payments, invoices)

    result: list[dict[str, Any]] = [
        anomaly.model_dump(mode="json") for anomaly in anomalies
    ]
    return _clip_and_dump(result)


@tool
async def run_payment_compliance_check(
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """执行付款合规性检查，检测逾期付款、提前付款和折扣滥用。

    将付款数据与发票数据关联，检查付款日期是否符合合同约定。

    Args:
        supplier_id: 供应商 ID，为空则检查所有供应商。
        days: 检查最近 N 天内的付款，默认 30 天。

    Returns:
        JSON 格式的付款合规性异常列表字符串。
    """
    return await asyncio.to_thread(
        _run_payment_compliance_check_sync, supplier_id, days
    )


def _calculate_supplier_kpis_sync(supplier_id: str, period: str) -> str:
    """同步计算供应商 KPI 的真实实现。"""
    settings = get_settings()
    repo = _get_repository()

    po_lines = repo.get_flattened_purchase_orders(supplier_id=supplier_id)
    gr_lines = repo.get_flattened_receipts(supplier_id=supplier_id)
    invoices = repo.get_flattened_invoices(supplier_id=supplier_id)

    # 获取供应商名称
    supplier_name: str = ""
    if po_lines:
        supplier_name = po_lines[0].get("supplier_name", supplier_id)

    if not period:
        period = "近30天"

    calculator = SupplierPerformanceCalculator(settings.p2p)
    report = calculator.calculate(
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        po_lines=po_lines,
        gr_lines=gr_lines,
        invoices=invoices,
        period=period,
    )

    return _clip_and_dump(report.model_dump(mode="json"))


@tool
async def calculate_supplier_kpis(
    supplier_id: str,
    period: str = "",
) -> str:
    """计算供应商绩效 KPI，包括准时交付率、发票准确率、质检合格率、价格合规率。

    基于采购订单、收货、发票数据综合计算四项核心 KPI 指标，
    并与配置基准值比较生成状态评级。

    Args:
        supplier_id: 供应商 ID（必填）。
        period: 评估周期描述，如 "2026-Q1"，为空则使用 "近30天"。

    Returns:
        JSON 格式的供应商 KPI 报告字符串。
    """
    return await asyncio.to_thread(_calculate_supplier_kpis_sync, supplier_id, period)


# ============================================================
# 新增工具（Phase 2）
# ============================================================

_tool_logger: Any = None


def _get_tool_logger() -> Any:
    """延迟获取 logger，避免模块级循环导入。"""
    global _tool_logger
    if _tool_logger is None:
        from core.logging_utils import get_logger
        _tool_logger = get_logger(__name__)
    return _tool_logger


@tool
async def query_vendor_master(vendor_ids: str = "") -> str:
    """查询供应商主数据信息。

    返回供应商基本信息，包括名称、站点、付款条款、状态等。

    Args:
        vendor_ids: 逗号分隔的供应商 ID 列表，为空则返回全部。

    Returns:
        JSON 格式的供应商主数据列表字符串。
    """
    repo = _get_repository()
    ids = [v.strip() for v in vendor_ids.split(",") if v.strip()] if vendor_ids else []
    suppliers = await asyncio.to_thread(_query_vendor_master_sync, repo, ids)
    return _clip_and_dump(suppliers)


def _query_vendor_master_sync(repo: P2PRepository, vendor_ids: list[str]) -> list[dict[str, Any]]:
    """同步查询供应商主数据。"""
    from sqlalchemy import select
    from core.database.models import ApSupplier

    with repo._session_factory() as session:
        stmt = select(
            ApSupplier.supplier_id,
            ApSupplier.supplier_name,
            ApSupplier.supplier_site_id,
            ApSupplier.payment_terms,
            ApSupplier.status,
        )
        if vendor_ids:
            stmt = stmt.where(ApSupplier.supplier_id.in_(vendor_ids))

        rows = session.execute(stmt).all()
        return [
            {
                "supplier_id": r.supplier_id,
                "supplier_name": r.supplier_name,
                "supplier_site_id": r.supplier_site_id,
                "payment_terms": r.payment_terms,
                "status": r.status,
            }
            for r in rows
        ]


@tool
async def calculate_spend_analysis(
    group_by: str = "category",
    days: int = 30,
) -> str:
    """按维度聚合采购支出分析。

    对采购订单金额按指定维度（供应商或品类）进行汇总统计。

    Args:
        group_by: 聚合维度，"category"（按品类）或 "supplier"（按供应商），默认 category。
        days: 分析最近 N 天内的数据，默认 30 天。

    Returns:
        JSON 格式的支出分析结果字符串。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(_calculate_spend_analysis_sync, repo, group_by, days)
    return _clip_and_dump(result)


def _calculate_spend_analysis_sync(
    repo: P2PRepository, group_by: str, days: int
) -> list[dict[str, Any]]:
    """同步执行采购支出聚合分析。"""
    from datetime import date, timedelta
    from sqlalchemy import func, select
    from core.database.models import PoHeader, PoLine

    cutoff = date.today() - timedelta(days=days)

    with repo._session_factory() as session:
        if group_by == "supplier":
            group_col = PoHeader.supplier_name
        else:
            group_col = PoLine.category

        stmt = (
            select(
                group_col.label("group_key"),
                func.count(func.distinct(PoHeader.po_number)).label("order_count"),
                func.sum(PoLine.amount).label("total_amount"),
                func.avg(PoLine.unit_price).label("avg_unit_price"),
            )
            .join(PoLine, PoHeader.po_header_id == PoLine.po_header_id)
            .where(PoHeader.creation_date >= cutoff)
            .group_by(group_col)
            .order_by(func.sum(PoLine.amount).desc())
        )

        rows = session.execute(stmt).all()
        return [
            {
                "group_by": group_by,
                "group_key": str(r.group_key),
                "order_count": r.order_count,
                "total_amount": float(r.total_amount) if r.total_amount else 0.0,
                "avg_unit_price": round(float(r.avg_unit_price), 2) if r.avg_unit_price else 0.0,
            }
            for r in rows
        ]


# ── 新增分析工具（第一梯队场景扩展） ──────────────────────────────────


@tool
async def analyze_receipt_anomalies(
    supplier_id: str = "",
    po_number: str = "",
    days: int = 30,
) -> str:
    """分析收货异常：超量收货、拒收、延迟收货。

    对比 PO 行数量与收货数量，检测超量收货（>PO 数量）、
    拒收（rejected_quantity > 0）、延迟收货（收货日期 > 承诺日期）。

    Args:
        supplier_id: 供应商 ID，空则分析全部。
        po_number: 采购订单号，空则分析全部。
        days: 分析最近 N 天。

    Returns:
        JSON 格式的收货异常列表。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(
        _analyze_receipt_anomalies_sync, repo, supplier_id, po_number, days
    )
    return _clip_and_dump(result)


def _analyze_receipt_anomalies_sync(
    repo: P2PRepository, supplier_id: str, po_number: str, days: int
) -> list[dict[str, Any]]:
    """同步执行收货异常分析。"""
    from datetime import date, timedelta
    from sqlalchemy import select
    from core.database.models import PoLine, PoLineLocation, RcvTransaction

    cutoff = date.today() - timedelta(days=days)
    anomalies: list[dict[str, Any]] = []

    with repo._session_factory() as session:
        stmt = select(RcvTransaction)
        if supplier_id:
            stmt = stmt.where(RcvTransaction.supplier_id == supplier_id)
        if po_number:
            stmt = stmt.where(RcvTransaction.po_number == po_number)
        stmt = stmt.where(RcvTransaction.transaction_date >= cutoff)
        receipts = session.scalars(stmt).all()

        for rcv in receipts:
            po_line = session.get(PoLine, rcv.po_line_id)
            if po_line is None:
                continue

            issues: list[str] = []

            # 超量收货
            if rcv.quantity > po_line.quantity:
                over_pct = ((rcv.quantity - po_line.quantity) / po_line.quantity) * 100
                issues.append(f"超量收货 {over_pct:.1f}%")

            # 拒收
            if rcv.rejected_quantity > 0:
                reject_pct = (rcv.rejected_quantity / rcv.quantity) * 100
                issues.append(f"拒收 {rcv.rejected_quantity} 件 ({reject_pct:.1f}%)")

            # 延迟收货
            loc = session.execute(
                select(PoLineLocation)
                .where(PoLineLocation.po_line_id == rcv.po_line_id)
                .limit(1)
            ).scalar()
            if loc and rcv.transaction_date > loc.promised_date:
                delay = (rcv.transaction_date - loc.promised_date).days
                issues.append(f"延迟 {delay} 天")

            if issues:
                anomalies.append({
                    "receipt_id": f"GR-{rcv.transaction_id:04d}",
                    "po_number": rcv.po_number,
                    "supplier_id": rcv.supplier_id,
                    "po_quantity": float(po_line.quantity),
                    "received_quantity": float(rcv.quantity),
                    "rejected_quantity": float(rcv.rejected_quantity),
                    "receipt_date": rcv.transaction_date.isoformat(),
                    "issues": issues,
                })

    return anomalies


@tool
async def detect_duplicate_invoices(
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """检测重复发票：同供应商、同金额、同日期（±3天）的发票对。

    Args:
        supplier_id: 供应商 ID，空则检查全部。
        days: 分析最近 N 天。

    Returns:
        JSON 格式的疑似重复发票列表。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(
        _detect_duplicate_invoices_sync, repo, supplier_id, days
    )
    return _clip_and_dump(result)


def _detect_duplicate_invoices_sync(
    repo: P2PRepository, supplier_id: str, days: int
) -> list[dict[str, Any]]:
    """同步执行发票重复检测。"""
    from datetime import date, timedelta
    from sqlalchemy import select
    from core.database.models import ApInvoice

    cutoff = date.today() - timedelta(days=days)
    duplicates: list[dict[str, Any]] = []

    with repo._session_factory() as session:
        stmt = select(ApInvoice).where(ApInvoice.invoice_date >= cutoff)
        if supplier_id:
            stmt = stmt.where(ApInvoice.supplier_id == supplier_id)
        invoices = session.scalars(stmt).all()

        # 按 (supplier_id, invoice_amount) 分组
        groups: dict[tuple[str, float], list[Any]] = {}
        for inv in invoices:
            key = (inv.supplier_id, float(inv.invoice_amount))
            groups.setdefault(key, []).append(inv)

        for (sid, amount), group in groups.items():
            if len(group) < 2:
                continue
            # 检查日期接近度（±3天）
            group.sort(key=lambda x: x.invoice_date)
            for i in range(len(group) - 1):
                for j in range(i + 1, len(group)):
                    gap = abs((group[j].invoice_date - group[i].invoice_date).days)
                    if gap <= 3:
                        duplicates.append({
                            "supplier_id": sid,
                            "supplier_name": group[i].supplier_name,
                            "amount": amount,
                            "invoice_a": group[i].invoice_number,
                            "invoice_b": group[j].invoice_number,
                            "date_a": group[i].invoice_date.isoformat(),
                            "date_b": group[j].invoice_date.isoformat(),
                            "date_gap_days": gap,
                        })

    return duplicates


@tool
async def analyze_discount_utilization(
    supplier_id: str = "",
    days: int = 30,
) -> str:
    """分析早付折扣利用率：哪些发票有折扣机会但未利用。

    检查有 discount_due_date 的发票，对比实际付款日期，
    计算折扣利用率和因未利用折扣损失的金额。

    Args:
        supplier_id: 供应商 ID，空则分析全部。
        days: 分析最近 N 天。

    Returns:
        JSON 格式的折扣利用分析结果。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(
        _analyze_discount_utilization_sync, repo, supplier_id, days
    )
    return _clip_and_dump(result)


def _analyze_discount_utilization_sync(
    repo: P2PRepository, supplier_id: str, days: int
) -> dict[str, Any]:
    """同步执行折扣利用率分析。"""
    from datetime import date, timedelta
    from sqlalchemy import select
    from core.database.models import ApInvoice, ApPayment

    cutoff = date.today() - timedelta(days=days)
    discount_rate = 0.02  # 假定标准 2% 折扣

    with repo._session_factory() as session:
        stmt = (
            select(ApInvoice)
            .where(
                ApInvoice.invoice_date >= cutoff,
                ApInvoice.discount_due_date.isnot(None),
            )
        )
        if supplier_id:
            stmt = stmt.where(ApInvoice.supplier_id == supplier_id)
        invoices = session.scalars(stmt).all()

        total_eligible = len(invoices)
        utilized = 0
        missed = 0
        missed_amount = 0.0
        details: list[dict[str, Any]] = []

        for inv in invoices:
            payment = session.execute(
                select(ApPayment)
                .where(ApPayment.invoice_number == inv.invoice_number)
                .limit(1)
            ).scalar()

            if payment is None:
                continue

            potential_saving = float(inv.invoice_amount) * discount_rate
            if payment.payment_date <= inv.discount_due_date:
                utilized += 1
            else:
                missed += 1
                missed_amount += potential_saving
                details.append({
                    "invoice_number": inv.invoice_number,
                    "supplier_name": inv.supplier_name,
                    "invoice_amount": float(inv.invoice_amount),
                    "discount_due_date": inv.discount_due_date.isoformat(),
                    "payment_date": payment.payment_date.isoformat(),
                    "days_late": (payment.payment_date - inv.discount_due_date).days,
                    "missed_saving": round(potential_saving, 2),
                })

        utilization_rate = (utilized / total_eligible * 100) if total_eligible > 0 else 0

    return {
        "total_eligible": total_eligible,
        "utilized": utilized,
        "missed": missed,
        "utilization_rate": round(utilization_rate, 1),
        "total_missed_saving": round(missed_amount, 2),
        "missed_details": details[:20],  # 最多返回 20 条
    }


@tool
async def analyze_vendor_concentration(
    days: int = 30,
    top_n: int = 10,
) -> str:
    """分析供应商集中度：采购依赖度和单一来源风险。

    计算各供应商的采购占比，识别高依赖供应商（>20% 占比）
    和单一来源品类（某品类仅 1 家供应商）。

    Args:
        days: 分析最近 N 天。
        top_n: 返回 Top N 供应商。

    Returns:
        JSON 格式的供应商集中度分析结果。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(
        _analyze_vendor_concentration_sync, repo, days, top_n
    )
    return _clip_and_dump(result)


def _analyze_vendor_concentration_sync(
    repo: P2PRepository, days: int, top_n: int
) -> dict[str, Any]:
    """同步执行供应商集中度分析。"""
    from datetime import date, timedelta
    from sqlalchemy import func, select
    from core.database.models import PoHeader, PoLine

    cutoff = date.today() - timedelta(days=days)

    with repo._session_factory() as session:
        # 按供应商汇总支出
        stmt = (
            select(
                PoHeader.supplier_id,
                PoHeader.supplier_name,
                func.sum(PoHeader.total_amount).label("total_spend"),
                func.count(func.distinct(PoHeader.po_number)).label("order_count"),
            )
            .where(PoHeader.creation_date >= cutoff)
            .group_by(PoHeader.supplier_id, PoHeader.supplier_name)
            .order_by(func.sum(PoHeader.total_amount).desc())
        )
        vendor_rows = session.execute(stmt).all()

        grand_total = sum(float(r.total_spend or 0) for r in vendor_rows)
        vendors = []
        high_dependency: list[dict[str, Any]] = []
        for r in vendor_rows[:top_n]:
            spend = float(r.total_spend or 0)
            pct = (spend / grand_total * 100) if grand_total > 0 else 0
            entry = {
                "supplier_id": r.supplier_id,
                "supplier_name": r.supplier_name,
                "total_spend": round(spend, 2),
                "order_count": r.order_count,
                "spend_pct": round(pct, 1),
            }
            vendors.append(entry)
            if pct > 20:
                high_dependency.append(entry)

        # 单一来源品类
        cat_stmt = (
            select(
                PoLine.category,
                func.count(func.distinct(PoHeader.supplier_id)).label("vendor_count"),
            )
            .join(PoHeader, PoHeader.po_header_id == PoLine.po_header_id)
            .where(PoHeader.creation_date >= cutoff)
            .group_by(PoLine.category)
            .having(func.count(func.distinct(PoHeader.supplier_id)) == 1)
        )
        single_source = session.execute(cat_stmt).all()
        single_source_categories = [r.category for r in single_source]

    return {
        "grand_total_spend": round(grand_total, 2),
        "top_vendors": vendors,
        "high_dependency_vendors": high_dependency,
        "single_source_categories": single_source_categories,
    }


# ── 存根工具（Phase 4 真实实现） ─────────────────────────────────────


@tool
async def query_material_master(material_ids: str = "") -> str:
    """查询物料主数据信息（存根，Phase 4 实现）。

    Args:
        material_ids: 逗号分隔的物料 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("query_material_master is a stub, returning empty result")
    return "[]"


@tool
async def calculate_po_cycle_time(
    days: int = 30,
    supplier_id: str = "",
) -> str:
    """计算采购订单全流程周期：创建→收货→付款各阶段耗时。

    Args:
        days: 分析最近 N 天的采购订单。
        supplier_id: 供应商 ID，空则分析全部。

    Returns:
        JSON 格式的周期分析结果。
    """
    repo = _get_repository()
    result = await asyncio.to_thread(
        _calculate_po_cycle_time_sync, repo, days, supplier_id
    )
    return _clip_and_dump(result)


def _calculate_po_cycle_time_sync(
    repo: P2PRepository, days: int, supplier_id: str
) -> dict[str, Any]:
    """同步执行 PO 周期分析。"""
    from datetime import date, timedelta
    from sqlalchemy import func, select
    from core.database.models import ApInvoice, ApPayment, PoHeader, RcvTransaction

    cutoff = date.today() - timedelta(days=days)

    with repo._session_factory() as session:
        stmt = select(PoHeader).where(PoHeader.creation_date >= cutoff)
        if supplier_id:
            stmt = stmt.where(PoHeader.supplier_id == supplier_id)
        orders = session.scalars(stmt).all()

        details: list[dict[str, Any]] = []
        total_create_to_receipt = 0
        total_receipt_to_payment = 0
        total_e2e = 0
        count_with_receipt = 0
        count_with_payment = 0

        for po in orders:
            entry: dict[str, Any] = {
                "po_number": po.po_number,
                "supplier_name": po.supplier_name,
                "created_date": po.creation_date.isoformat(),
            }

            # 最早收货日期
            first_receipt = session.execute(
                select(func.min(RcvTransaction.transaction_date))
                .where(RcvTransaction.po_number == po.po_number)
            ).scalar()

            if first_receipt:
                create_to_receipt = (first_receipt - po.creation_date).days
                entry["first_receipt_date"] = first_receipt.isoformat()
                entry["create_to_receipt_days"] = create_to_receipt
                total_create_to_receipt += create_to_receipt
                count_with_receipt += 1

                # 最早付款日期（通过发票关联）
                first_payment_date = session.execute(
                    select(func.min(ApPayment.payment_date))
                    .join(ApInvoice, ApPayment.invoice_number == ApInvoice.invoice_number)
                    .where(ApInvoice.po_number == po.po_number)
                ).scalar()

                if first_payment_date:
                    receipt_to_payment = (first_payment_date - first_receipt).days
                    e2e = (first_payment_date - po.creation_date).days
                    entry["first_payment_date"] = first_payment_date.isoformat()
                    entry["receipt_to_payment_days"] = receipt_to_payment
                    entry["end_to_end_days"] = e2e
                    total_receipt_to_payment += receipt_to_payment
                    total_e2e += e2e
                    count_with_payment += 1

            details.append(entry)

    return {
        "total_orders": len(details),
        "avg_create_to_receipt_days": (
            round(total_create_to_receipt / count_with_receipt, 1)
            if count_with_receipt > 0 else None
        ),
        "avg_receipt_to_payment_days": (
            round(total_receipt_to_payment / count_with_payment, 1)
            if count_with_payment > 0 else None
        ),
        "avg_end_to_end_days": (
            round(total_e2e / count_with_payment, 1)
            if count_with_payment > 0 else None
        ),
        "orders_with_receipt": count_with_receipt,
        "orders_with_payment": count_with_payment,
        "details": details[:30],
    }


@tool
async def run_vendor_risk_scoring(vendor_ids: str = "") -> str:
    """执行供应商风险评分（存根，Phase 4 实现）。

    Args:
        vendor_ids: 逗号分隔的供应商 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("run_vendor_risk_scoring is a stub, returning empty result")
    return "[]"


@tool
async def check_approval_limits(po_ids: str = "") -> str:
    """检查采购审批限额合规性（存根，Phase 4 实现）。

    Args:
        po_ids: 逗号分隔的采购订单 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("check_approval_limits is a stub, returning empty result")
    return "[]"


@tool
async def check_blacklist(vendor_ids: str = "") -> str:
    """检查供应商黑名单（存根，Phase 4 实现）。

    Args:
        vendor_ids: 逗号分隔的供应商 ID 列表。

    Returns:
        空列表 JSON。
    """
    _get_tool_logger().warning("check_blacklist is a stub, returning empty result")
    return "[]"
