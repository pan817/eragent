"""Receiving domain extractor: RCV_SHIPMENT_HEADERS, RCV_SHIPMENT_LINES, RCV_TRANSACTIONS."""

from __future__ import annotations

from typing import Any

from core.database.models import RcvShipmentHeader, RcvShipmentLine, RcvTransaction
from core.etl.extractors.base import BaseExtractor

_TABLE_MODELS: dict[str, type] = {
    "RCV_SHIPMENT_HEADERS": RcvShipmentHeader,
    "RCV_SHIPMENT_LINES": RcvShipmentLine,
    "RCV_TRANSACTIONS": RcvTransaction,
}


class ReceivingExtractor(BaseExtractor):
    """Extracts shipment header, line, and receipt transaction data."""

    def table_names(self) -> list[str]:
        return list(_TABLE_MODELS)

    def domain(self) -> str:
        return "receiving"

    def _model_for_table(self, table_name: str) -> type:
        return _TABLE_MODELS[table_name]

    def _row_to_dict(self, table_name: str, row: Any) -> dict[str, Any]:
        if table_name == "RCV_SHIPMENT_HEADERS":
            return self._header_to_dict(row)
        if table_name == "RCV_SHIPMENT_LINES":
            return self._line_to_dict(row)
        return self._txn_to_dict(row)

    @staticmethod
    def _header_to_dict(r: RcvShipmentHeader) -> dict[str, Any]:
        return {
            "shipment_header_id": r.shipment_header_id,
            "receipt_num": r.receipt_num,
            "vendor_id": r.vendor_id,
            "vendor_site_id": r.vendor_site_id,
            "ship_to_org_id": r.ship_to_org_id,
            "shipped_date": r.shipped_date,
            "expected_receipt_date": r.expected_receipt_date,
            "receipt_source_code": r.receipt_source_code,
            "freight_carrier_code": r.freight_carrier_code,
            "comments": r.comments,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _line_to_dict(r: RcvShipmentLine) -> dict[str, Any]:
        return {
            "shipment_line_id": r.shipment_line_id,
            "shipment_header_id": r.shipment_header_id,
            "line_num": r.line_num,
            "po_header_id": r.po_header_id,
            "po_line_id": r.po_line_id,
            "item_id": r.item_id,
            "item_description": r.item_description,
            "quantity_shipped": float(r.quantity_shipped)
            if r.quantity_shipped is not None
            else None,
            "quantity_received": float(r.quantity_received)
            if r.quantity_received is not None
            else None,
            "unit_of_measure": r.unit_of_measure,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _txn_to_dict(r: RcvTransaction) -> dict[str, Any]:
        return {
            "transaction_id": r.transaction_id,
            "shipment_header_id": r.shipment_header_id,
            "po_number": r.po_number,
            "po_line_id": r.po_line_id,
            "transaction_type": r.transaction_type,
            "quantity": r.quantity,
            "accepted_quantity": r.accepted_quantity,
            "rejected_quantity": r.rejected_quantity,
            "transaction_date": r.transaction_date,
            "vendor_id": r.vendor_id,
            "shipment_line_id": r.shipment_line_id,
            "source_document_code": r.source_document_code,
            "destination_type_code": r.destination_type_code,
            "inspection_status_code": r.inspection_status_code,
            "last_update_date": r.last_update_date,
        }
