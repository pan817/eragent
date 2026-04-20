"""Payables domain extractor: AP_INVOICES, AP_INVOICE_LINES, AP_INVOICE_DISTRIBUTIONS,
AP_CHECKS, AP_INVOICE_PAYMENTS, AP_PAYMENT_SCHEDULES."""

from __future__ import annotations

from typing import Any

from core.database.models import (
    ApInvoice,
    ApInvoiceDistribution,
    ApInvoiceLine,
    ApInvoicePayment,
    ApPayment,
    ApPaymentSchedule,
)
from core.etl.extractors.base import BaseExtractor

_TABLE_MODELS: dict[str, type] = {
    "AP_INVOICES_ALL": ApInvoice,
    "AP_INVOICE_LINES_ALL": ApInvoiceLine,
    "AP_INVOICE_DISTRIBUTIONS_ALL": ApInvoiceDistribution,
    "AP_CHECKS_ALL": ApPayment,
    "AP_INVOICE_PAYMENTS_ALL": ApInvoicePayment,
    "AP_PAYMENT_SCHEDULES_ALL": ApPaymentSchedule,
}


class PayablesExtractor(BaseExtractor):
    """Extracts invoice, payment, and payment-schedule data."""

    def table_names(self) -> list[str]:
        return list(_TABLE_MODELS)

    def domain(self) -> str:
        return "payables"

    def _model_for_table(self, table_name: str) -> type:
        return _TABLE_MODELS[table_name]

    def _row_to_dict(self, table_name: str, row: Any) -> dict[str, Any]:
        converters = {
            "AP_INVOICES_ALL": self._invoice_to_dict,
            "AP_INVOICE_LINES_ALL": self._inv_line_to_dict,
            "AP_INVOICE_DISTRIBUTIONS_ALL": self._inv_dist_to_dict,
            "AP_CHECKS_ALL": self._payment_to_dict,
            "AP_INVOICE_PAYMENTS_ALL": self._inv_pmt_to_dict,
            "AP_PAYMENT_SCHEDULES_ALL": self._pmt_schedule_to_dict,
        }
        return converters[table_name](row)

    @staticmethod
    def _invoice_to_dict(r: ApInvoice) -> dict[str, Any]:
        return {
            "invoice_id": r.invoice_id,
            "invoice_num": r.invoice_num,
            "po_number": r.po_number,
            "vendor_id": r.vendor_id,
            "vendor_name": r.vendor_name,
            "invoice_amount": float(r.invoice_amount),
            "invoice_date": r.invoice_date,
            "due_date": r.due_date,
            "discount_due_date": r.discount_due_date,
            "approval_status": r.approval_status,
            "terms_id": r.terms_id,
            "invoice_type_lookup_code": r.invoice_type_lookup_code,
            "invoice_currency_code": r.invoice_currency_code,
            "amount_paid": float(r.amount_paid) if r.amount_paid is not None else None,
            "cancelled_date": r.cancelled_date,
            "description": r.description,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _inv_line_to_dict(r: ApInvoiceLine) -> dict[str, Any]:
        return {
            "invoice_line_id": r.invoice_line_id,
            "invoice_id": r.invoice_id,
            "line_number": r.line_number,
            "line_type_lookup_code": r.line_type_lookup_code,
            "amount": float(r.amount) if r.amount is not None else None,
            "quantity_invoiced": float(r.quantity_invoiced)
            if r.quantity_invoiced is not None
            else None,
            "unit_price": float(r.unit_price) if r.unit_price is not None else None,
            "po_header_id": r.po_header_id,
            "po_line_id": r.po_line_id,
            "inventory_item_id": r.inventory_item_id,
            "item_description": r.item_description,
            "accounting_date": r.accounting_date,
            "description": r.description,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _inv_dist_to_dict(r: ApInvoiceDistribution) -> dict[str, Any]:
        return {
            "invoice_distribution_id": r.invoice_distribution_id,
            "invoice_id": r.invoice_id,
            "invoice_line_number": r.invoice_line_number,
            "amount": float(r.amount) if r.amount is not None else None,
            "po_distribution_id": r.po_distribution_id,
            "accounting_date": r.accounting_date,
            "match_status_flag": r.match_status_flag,
            "posted_flag": r.posted_flag,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _payment_to_dict(r: ApPayment) -> dict[str, Any]:
        return {
            "check_id": r.check_id,
            "check_number": r.check_number,
            "invoice_num": r.invoice_num,
            "vendor_id": r.vendor_id,
            "amount": float(r.amount),
            "check_date": r.check_date,
            "payment_method_code": r.payment_method_code,
            "status_lookup_code": r.status_lookup_code,
            "cleared_date": r.cleared_date,
            "void_date": r.void_date,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _inv_pmt_to_dict(r: ApInvoicePayment) -> dict[str, Any]:
        return {
            "invoice_payment_id": r.invoice_payment_id,
            "invoice_id": r.invoice_id,
            "check_id": r.check_id,
            "payment_num": r.payment_num,
            "amount": float(r.amount) if r.amount is not None else None,
            "discount_taken": float(r.discount_taken)
            if r.discount_taken is not None
            else None,
            "discount_lost": float(r.discount_lost)
            if r.discount_lost is not None
            else None,
            "accounting_date": r.accounting_date,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _pmt_schedule_to_dict(r: ApPaymentSchedule) -> dict[str, Any]:
        return {
            "payment_schedule_id": r.payment_schedule_id,
            "invoice_id": r.invoice_id,
            "payment_num": r.payment_num,
            "due_date": r.due_date,
            "discount_date": r.discount_date,
            "gross_amount": float(r.gross_amount)
            if r.gross_amount is not None
            else None,
            "amount_remaining": float(r.amount_remaining)
            if r.amount_remaining is not None
            else None,
            "payment_priority": r.payment_priority,
            "hold_flag": r.hold_flag,
            "payment_status_flag": r.payment_status_flag,
            "last_update_date": r.last_update_date,
        }
