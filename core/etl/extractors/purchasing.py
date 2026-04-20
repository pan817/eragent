"""Purchasing domain extractor: PO_HEADERS, PO_LINES, PO_LINE_LOCATIONS, PO_DISTRIBUTIONS."""

from __future__ import annotations

from typing import Any

from core.database.models import PoDistribution, PoHeader, PoLine, PoLineLocation
from core.etl.extractors.base import BaseExtractor

_TABLE_MODELS: dict[str, type] = {
    "PO_HEADERS_ALL": PoHeader,
    "PO_LINES_ALL": PoLine,
    "PO_LINE_LOCATIONS_ALL": PoLineLocation,
    "PO_DISTRIBUTIONS_ALL": PoDistribution,
}


class PurchasingExtractor(BaseExtractor):
    """Extracts purchase-order header, line, location, and distribution data."""

    def table_names(self) -> list[str]:
        return list(_TABLE_MODELS)

    def domain(self) -> str:
        return "purchasing"

    def _model_for_table(self, table_name: str) -> type:
        return _TABLE_MODELS[table_name]

    def _row_to_dict(self, table_name: str, row: Any) -> dict[str, Any]:
        if table_name == "PO_HEADERS_ALL":
            return self._header_to_dict(row)
        if table_name == "PO_LINES_ALL":
            return self._line_to_dict(row)
        if table_name == "PO_LINE_LOCATIONS_ALL":
            return self._location_to_dict(row)
        return self._distribution_to_dict(row)

    @staticmethod
    def _header_to_dict(r: PoHeader) -> dict[str, Any]:
        return {
            "po_header_id": r.po_header_id,
            "po_number": r.po_number,
            "vendor_id": r.vendor_id,
            "vendor_name": r.vendor_name,
            "status": r.status,
            "creation_date": r.creation_date,
            "total_amount": float(r.total_amount),
            "currency": r.currency,
            "type_lookup_code": r.type_lookup_code,
            "revision_num": r.revision_num,
            "approved_date": r.approved_date,
            "authorization_status": r.authorization_status,
            "buyer_id": r.buyer_id,
            "org_id": r.org_id,
            "comments": r.comments,
            "blanket_total_amount": float(r.blanket_total_amount)
            if r.blanket_total_amount is not None
            else None,
            "agent_id": r.agent_id,
            "closed_code": r.closed_code,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _line_to_dict(r: PoLine) -> dict[str, Any]:
        return {
            "po_line_id": r.po_line_id,
            "po_header_id": r.po_header_id,
            "po_number": r.po_number,
            "line_num": r.line_num,
            "item_id": r.item_id,
            "item_description": r.item_description,
            "quantity": r.quantity,
            "unit_price": float(r.unit_price),
            "amount": float(r.amount),
            "category_id": r.category_id,
            "standard_price": float(r.standard_price),
            "unit_meas_lookup_code": r.unit_meas_lookup_code,
            "from_header_id": r.from_header_id,
            "from_line_id": r.from_line_id,
            "closed_code": r.closed_code,
            "contract_num": r.contract_num,
            "vendor_product_num": r.vendor_product_num,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _location_to_dict(r: PoLineLocation) -> dict[str, Any]:
        return {
            "line_location_id": r.line_location_id,
            "po_line_id": r.po_line_id,
            "po_number": r.po_number,
            "promised_date": r.promised_date,
            "need_by_date": r.need_by_date,
            "quantity": r.quantity,
            "shipment_num": r.shipment_num,
            "ship_to_location_id": r.ship_to_location_id,
            "quantity_received": float(r.quantity_received)
            if r.quantity_received is not None
            else None,
            "quantity_billed": float(r.quantity_billed)
            if r.quantity_billed is not None
            else None,
            "inspection_required_flag": r.inspection_required_flag,
            "receipt_required_flag": r.receipt_required_flag,
            "closed_code": r.closed_code,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _distribution_to_dict(r: PoDistribution) -> dict[str, Any]:
        return {
            "po_distribution_id": r.po_distribution_id,
            "po_header_id": r.po_header_id,
            "po_line_id": r.po_line_id,
            "line_location_id": r.line_location_id,
            "quantity_ordered": float(r.quantity_ordered)
            if r.quantity_ordered is not None
            else None,
            "quantity_delivered": float(r.quantity_delivered)
            if r.quantity_delivered is not None
            else None,
            "quantity_billed": float(r.quantity_billed)
            if r.quantity_billed is not None
            else None,
            "destination_type_code": r.destination_type_code,
            "org_id": r.org_id,
            "last_update_date": r.last_update_date,
        }
