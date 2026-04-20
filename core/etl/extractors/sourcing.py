"""Sourcing/contract domain extractor: PON_AUCTION_HEADERS, PON_BID_HEADERS,
OKC_K_HEADERS, OKC_K_LINES."""

from __future__ import annotations

from typing import Any

from core.database.models import OkcKHeader, OkcKLine, PonAuctionHeader, PonBidHeader
from core.etl.extractors.base import BaseExtractor

_TABLE_MODELS: dict[str, type] = {
    "PON_AUCTION_HEADERS_ALL": PonAuctionHeader,
    "PON_BID_HEADERS": PonBidHeader,
    "OKC_K_HEADERS_B": OkcKHeader,
    "OKC_K_LINES_B": OkcKLine,
}


class SourcingExtractor(BaseExtractor):
    """Extracts auction, bid, contract-header, and contract-line data."""

    def table_names(self) -> list[str]:
        return list(_TABLE_MODELS)

    def domain(self) -> str:
        return "sourcing"

    def _model_for_table(self, table_name: str) -> type:
        return _TABLE_MODELS[table_name]

    def _row_to_dict(self, table_name: str, row: Any) -> dict[str, Any]:
        converters = {
            "PON_AUCTION_HEADERS_ALL": self._auction_to_dict,
            "PON_BID_HEADERS": self._bid_to_dict,
            "OKC_K_HEADERS_B": self._contract_to_dict,
            "OKC_K_LINES_B": self._contract_line_to_dict,
        }
        return converters[table_name](row)

    @staticmethod
    def _auction_to_dict(r: PonAuctionHeader) -> dict[str, Any]:
        return {
            "auction_header_id": r.auction_header_id,
            "document_number": r.document_number,
            "auction_title": r.auction_title,
            "auction_type": r.auction_type,
            "auction_status": r.auction_status,
            "open_bidding_date": r.open_bidding_date,
            "close_bidding_date": r.close_bidding_date,
            "outcome": r.outcome,
            "contract_type": r.contract_type,
            "org_id": r.org_id,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _bid_to_dict(r: PonBidHeader) -> dict[str, Any]:
        return {
            "bid_number": r.bid_number,
            "auction_header_id": r.auction_header_id,
            "bid_status": r.bid_status,
            "vendor_id": r.vendor_id,
            "vendor_site_id": r.vendor_site_id,
            "bid_total": float(r.bid_total) if r.bid_total is not None else None,
            "bid_currency_code": r.bid_currency_code,
            "publish_date": r.publish_date,
            "award_status": r.award_status,
            "award_date": r.award_date,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _contract_to_dict(r: OkcKHeader) -> dict[str, Any]:
        return {
            "id": r.id,
            "contract_number": r.contract_number,
            "sts_code": r.sts_code,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "estimated_amount": float(r.estimated_amount)
            if r.estimated_amount is not None
            else None,
            "currency_code": r.currency_code,
            "buy_or_sell": r.buy_or_sell,
            "scs_code": r.scs_code,
            "description": r.description,
            "short_description": r.short_description,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _contract_line_to_dict(r: OkcKLine) -> dict[str, Any]:
        return {
            "id": r.id,
            "chr_id": r.chr_id,
            "line_number": r.line_number,
            "sts_code": r.sts_code,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "item_id": r.item_id,
            "item_description": r.item_description,
            "price_unit": float(r.price_unit) if r.price_unit is not None else None,
            "price_negotiated": float(r.price_negotiated)
            if r.price_negotiated is not None
            else None,
            "quantity": float(r.quantity) if r.quantity is not None else None,
            "uom_code": r.uom_code,
            "last_update_date": r.last_update_date,
        }
