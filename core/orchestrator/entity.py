"""实体处理：指代消解 + DB 验证 + 级联补充。

从 orchestrator.py 提取的实体处理管线，三个函数组成单向管线：
resolve_references → validate_entities → enrich_entities
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from core.logging_utils import get_logger

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 指代消解
# ---------------------------------------------------------------------------

# 指代前缀：覆盖中文常用的指示词 + 量词化指代 + 时间指代
_REF_PREFIX = (
    r"(?:这个|这条|这笔|这张|这批|这份|"
    r"该|那个|那条|那笔|那张|此|"
    r"上述|上面的|上面提到的|前面的|刚才的|之前的)"
)

# 指代模式表：(正则, 实体键, 替换文本模板)
_REF_PATTERNS: list[tuple[str, str, str]] = [
    (
        _REF_PREFIX + r"\s*(?:po|PO|订单|采购订单|采购单)",
        "po_number",
        "采购订单 {val}",
    ),
    (
        _REF_PREFIX + r"\s*(?:供应商|vendor|supplier)",
        "vendor_id",
        "供应商 {val}",
    ),
    (
        _REF_PREFIX + r"\s*(?:支付单|付款单|付款|payment|PAY|pay)",
        "check_number",
        "付款单 {val}",
    ),
    (
        _REF_PREFIX + r"\s*(?:发票|invoice|INV|inv)",
        "invoice_num",
        "发票 {val}",
    ),
    (
        _REF_PREFIX + r"\s*(?:收货单|收货|RCV|rcv|goods receipt)",
        "receipt_number",
        "收货单 {val}",
    ),
]

# 通用指代（未匹配具体实体类型时的兜底）
_GENERIC_REF = (
    r"(?:这个|这条|这笔|这张|这批|这份|"
    r"该|那个|那条|那笔|那张|此|它|"
    r"上述|上面的|上面提到的|前面的|刚才的|之前的)"
)


def resolve_references(
    query: str, session_ctx: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """指代消解：将 query 中的指代词替换为具体实体，返回 (增强 query, 选择性实体)。

    规则：
    - "这个/该/上述 + po/订单/采购订单" → 只关联 po_number
    - "这个/该/上述 + 供应商" → 只关联 vendor_id
    - 无指代但有历史 → 全部补充（保持兼容）
    - 无历史 → 原样返回

    返回：
    - enhanced_query: 指代词替换后的 query（供路由器使用）
    - relevant_entities: 与指代相关的实体子集（供参数补充使用）
    """
    from core.observability.tracing import record_span

    with record_span("entity", "resolve_references") as span_attrs:
        span_attrs["query"] = query
        span_attrs["has_history"] = session_ctx.get("has_history", False)

        if not session_ctx.get("has_history"):
            span_attrs["status"] = "skipped"
            span_attrs["reason"] = "no history"
            return query, {}

        entities = session_ctx.get("entities", {})
        if not entities:
            span_attrs["status"] = "skipped"
            span_attrs["reason"] = "no entities in history"
            return query, {}

        enhanced = query
        relevant: dict[str, Any] = {}

        for pattern, entity_key, replace_tpl in _REF_PATTERNS:
            ref_match = re.search(pattern, enhanced, re.IGNORECASE)
            if ref_match and entities.get(entity_key):
                val = entities[entity_key]
                replacement = replace_tpl.format(val=val)
                enhanced = enhanced[:ref_match.start()] + replacement + enhanced[ref_match.end():]
                relevant[entity_key] = val

        # 通用指代但未匹配到具体实体类型："分析这个的风险" / "它的情况"
        if not relevant:
            generic_ref = re.search(_GENERIC_REF + r"(?:的)", query)
            if generic_ref:
                relevant = entities.copy()

        span_attrs["enhanced_query"] = enhanced if enhanced != query else None
        span_attrs["resolved_entities"] = relevant or None
        span_attrs["status"] = "ok"

        # 无指代词时不做隐式继承——避免用户的新查询被静默限定到历史实体范围。
        return enhanced, relevant


# ---------------------------------------------------------------------------
# DB 验证
# ---------------------------------------------------------------------------


async def validate_entities(
    repo: Any, params: dict[str, Any]
) -> None:
    """验证正则提取的实体是否在 DB 中存在，不存在则清除。

    防止误匹配（如 "最近 10045 天" 中 10045 被当作 PO 号）。
    验证失败不阻塞主流程。
    """
    from core.observability.tracing import record_span

    with record_span("entity", "validate_entities") as span_attrs:
        input_entities = {k: v for k, v in params.items() if k != "days" and v}
        span_attrs["input"] = input_entities.copy()
        discarded: list[str] = []
        errors: list[str] = []

        # PO 号验证（days=0 不限时间——用户明确指定的 PO 可能创建于任何时间）
        po = params.get("po_number", "")
        if po:
            try:
                orders = await asyncio.to_thread(
                    repo.query_purchase_orders, po_number=po, days=0
                )
                if not orders:
                    _logger.info("entity validation: po_number=%s not found in DB, discarded", po)
                    del params["po_number"]
                    discarded.append(f"po_number={po}")
            except Exception as exc:
                _logger.warning("entity validation (po_number) failed: %s", exc)
                errors.append(f"po_number: {type(exc).__name__}: {exc}")

        # 供应商验证（days=0 不限时间）
        sid = params.get("vendor_id", "")
        if sid:
            try:
                orders = await asyncio.to_thread(
                    repo.query_purchase_orders, vendor_id=sid, days=0
                )
                if not orders:
                    _logger.info("entity validation: vendor_id=%s not found in DB, discarded", sid)
                    del params["vendor_id"]
                    discarded.append(f"vendor_id={sid}")
            except Exception as exc:
                _logger.warning("entity validation (vendor_id) failed: %s", exc)
                errors.append(f"vendor_id: {type(exc).__name__}: {exc}")

        # 发票号验证（days=0 不限时间）
        inv = params.get("invoice_num", "")
        if inv:
            try:
                invoices = await asyncio.to_thread(
                    repo.query_invoices, vendor_id="", status="", days=0
                )
                if not any(i.get("invoice_num") == inv for i in invoices):
                    _logger.info("entity validation: invoice_num=%s not found in DB, discarded", inv)
                    del params["invoice_num"]
                    discarded.append(f"invoice_num={inv}")
            except Exception as exc:
                _logger.warning("entity validation (invoice_num) failed: %s", exc)
                errors.append(f"invoice_num: {type(exc).__name__}: {exc}")

        # 付款号验证（days=0 不限时间）
        pay = params.get("check_number", "")
        if pay:
            try:
                payments = await asyncio.to_thread(
                    repo.query_payments, check_number=pay, days=0
                )
                if not payments:
                    _logger.info("entity validation: check_number=%s not found in DB, discarded", pay)
                    del params["check_number"]
                    discarded.append(f"check_number={pay}")
            except Exception as exc:
                _logger.warning("entity validation (check_number) failed: %s", exc)
                errors.append(f"check_number: {type(exc).__name__}: {exc}")

        # 收货号验证（days=0 不限时间）
        rcv = params.get("receipt_number", "")
        if rcv:
            try:
                receipts = await asyncio.to_thread(
                    repo.query_receipts, po_number="", vendor_id="", days=0
                )
                if not any(
                    r.get("receipt_id") == rcv or r.get("gr_number") == rcv
                    for r in receipts
                ):
                    _logger.info("entity validation: receipt_number=%s not found in DB, discarded", rcv)
                    del params["receipt_number"]
                    discarded.append(f"receipt_number={rcv}")
            except Exception as exc:
                _logger.warning("entity validation (receipt_number) failed: %s", exc)
                errors.append(f"receipt_number: {type(exc).__name__}: {exc}")

        validated = {k: v for k, v in params.items() if k != "days" and v}
        span_attrs["validated"] = validated
        span_attrs["discarded"] = discarded
        if errors:
            span_attrs["validation_errors"] = errors
            span_attrs["status"] = "warning"
        else:
            span_attrs["status"] = "ok"


# ---------------------------------------------------------------------------
# 级联补充
# ---------------------------------------------------------------------------


async def enrich_entities(
    params: dict[str, Any],
    provider: Any | None = None,
) -> None:
    """验证并补充实体关联。

    两阶段处理：
    阶段 1：DB 验证——正则提取的实体在 DB 中是否存在，不存在则清除（防误匹配）。
    阶段 2：级联补充——从已有实体反查关联实体。
      1. check_number → invoice_num（付款关联发票）
      2. receipt_number → po_number, vendor_id（收货关联 PO 和供应商）
      3. invoice_num → po_number, vendor_id（发票关联 PO 和供应商）
      4. po_number → vendor_id（PO 关联供应商）

    查询失败不阻塞主流程。
    """
    from core.observability.tracing import record_span

    try:
        if provider is not None:
            repo = provider.get_repository()
        else:
            from modules.p2p.tools import _get_repository
            repo = _get_repository()
    except Exception:
        return

    # ── 阶段 1：DB 验证 ──
    await validate_entities(repo, params)

    # ── 阶段 2：级联补充 ──
    with record_span("entity", "enrich_entities") as span_attrs:
        before = {k: v for k, v in params.items() if k != "days" and v}
        span_attrs["before"] = before.copy()
        enriched_pairs: list[str] = []
        errors: list[str] = []

        def _log_enriched(src: str, src_val: str, tgt: str, tgt_val: str) -> None:
            _logger.info("entity enriched: %s=%s → %s=%s", src, src_val, tgt, tgt_val)
            enriched_pairs.append(f"{src}={src_val}→{tgt}={tgt_val}")

        # 所有级联查询统一使用 days=0（不限时间）——用户指定的实体可能创建于任何时间

        # 1. check_number → invoice_num
        pay = params.get("check_number", "")
        if pay and not params.get("invoice_num"):
            try:
                payments = await asyncio.to_thread(
                    repo.query_payments, check_number=pay, days=0
                )
                if payments:
                    inv_num = payments[0].get("invoice_num", "")
                    if inv_num:
                        params["invoice_num"] = inv_num
                        _log_enriched("check_number", pay, "invoice_num", inv_num)
            except Exception as exc:
                _logger.warning("entity enrichment (payment→invoice) failed: %s", exc)
                errors.append(f"payment→invoice: {type(exc).__name__}: {exc}")

        # 2. receipt_number → po_number, vendor_id
        rcv = params.get("receipt_number", "")
        if rcv:
            try:
                receipts = await asyncio.to_thread(
                    repo.query_receipts, po_number="", vendor_id="", days=0
                )
                matched = [r for r in receipts if r.get("receipt_id") == rcv or r.get("gr_number") == rcv]
                if matched:
                    if not params.get("po_number"):
                        po_num = matched[0].get("po_number", "")
                        if po_num:
                            params["po_number"] = po_num
                            _log_enriched("receipt_number", rcv, "po_number", po_num)
                    if not params.get("vendor_id"):
                        sid = matched[0].get("vendor_id", "")
                        if sid:
                            params["vendor_id"] = sid
                            _log_enriched("receipt_number", rcv, "vendor_id", sid)
            except Exception as exc:
                _logger.warning("entity enrichment (receipt→po/supplier) failed: %s", exc)
                errors.append(f"receipt→po/supplier: {type(exc).__name__}: {exc}")

        # 3. invoice_num → po_number, vendor_id
        inv = params.get("invoice_num", "")
        if inv:
            try:
                invoices = await asyncio.to_thread(
                    repo.query_invoices, vendor_id="", status="", days=0
                )
                matched = [i for i in invoices if i.get("invoice_num") == inv]
                if matched:
                    if not params.get("po_number"):
                        po_num = matched[0].get("po_number", "")
                        if po_num:
                            params["po_number"] = po_num
                            _log_enriched("invoice_num", inv, "po_number", po_num)
                    if not params.get("vendor_id"):
                        sid = matched[0].get("vendor_id", "")
                        if sid:
                            params["vendor_id"] = sid
                            _log_enriched("invoice_num", inv, "vendor_id", sid)
            except Exception as exc:
                _logger.warning("entity enrichment (invoice→po/supplier) failed: %s", exc)
                errors.append(f"invoice→po/supplier: {type(exc).__name__}: {exc}")

        # 4. po_number → vendor_id
        po = params.get("po_number", "")
        if po and not params.get("vendor_id"):
            try:
                orders = await asyncio.to_thread(
                    repo.query_purchase_orders, po_number=po, days=0
                )
                if orders:
                    sid = orders[0].get("vendor_id", "")
                    if sid:
                        params["vendor_id"] = sid
                        _log_enriched("po_number", po, "vendor_id", sid)
            except Exception as exc:
                _logger.warning("entity enrichment (po→supplier) failed: %s", exc)
                errors.append(f"po→supplier: {type(exc).__name__}: {exc}")

        after = {k: v for k, v in params.items() if k != "days" and v}
        span_attrs["after"] = after
        span_attrs["enriched"] = enriched_pairs
        if errors:
            span_attrs["enrichment_errors"] = errors
            span_attrs["status"] = "warning"
        else:
            span_attrs["status"] = "ok"
