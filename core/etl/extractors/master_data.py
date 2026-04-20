"""Master-data domain extractor: AP_SUPPLIERS, AP_SUPPLIER_SITES, MTL_SYSTEM_ITEMS."""

from __future__ import annotations

from typing import Any

from core.database.models import ApSupplier, ApSupplierSite, MtlSystemItem
from core.etl.extractors.base import BaseExtractor

_TABLE_MODELS: dict[str, type] = {
    "AP_SUPPLIERS": ApSupplier,
    "AP_SUPPLIER_SITES_ALL": ApSupplierSite,
    "MTL_SYSTEM_ITEMS_B": MtlSystemItem,
}


class MasterDataExtractor(BaseExtractor):
    """Extracts supplier, supplier-site, and material master data."""

    def table_names(self) -> list[str]:
        return ["AP_SUPPLIERS", "AP_SUPPLIER_SITES_ALL", "MTL_SYSTEM_ITEMS_B"]

    def domain(self) -> str:
        return "master_data"

    def _model_for_table(self, table_name: str) -> type:
        return _TABLE_MODELS[table_name]

    def _row_to_dict(self, table_name: str, row: Any) -> dict[str, Any]:
        if table_name == "AP_SUPPLIERS":
            return self._supplier_to_dict(row)
        if table_name == "AP_SUPPLIER_SITES_ALL":
            return self._site_to_dict(row)
        return self._item_to_dict(row)

    # ------------------------------------------------------------------

    @staticmethod
    def _supplier_to_dict(r: ApSupplier) -> dict[str, Any]:
        return {
            "vendor_id": r.vendor_id,
            "vendor_name": r.vendor_name,
            "supplier_site_id": r.supplier_site_id,
            "terms_id": r.terms_id,
            "enabled_flag": r.enabled_flag,
            "segment1": r.segment1,
            "vendor_type_lookup_code": r.vendor_type_lookup_code,
            "start_date_active": r.start_date_active,
            "end_date_active": r.end_date_active,
            "standard_industry_class": r.standard_industry_class,
            "small_business_flag": r.small_business_flag,
            "women_owned_flag": r.women_owned_flag,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _site_to_dict(r: ApSupplierSite) -> dict[str, Any]:
        return {
            "vendor_site_id": r.vendor_site_id,
            "vendor_id": r.vendor_id,
            "vendor_site_code": r.vendor_site_code,
            "city": r.city,
            "state": r.state,
            "country": r.country,
            "phone": r.phone,
            "email_address": r.email_address,
            "terms_id": r.terms_id,
            "pay_group_lookup_code": r.pay_group_lookup_code,
            "payment_method_lookup_code": r.payment_method_lookup_code,
            "org_id": r.org_id,
            "purchasing_site_flag": r.purchasing_site_flag,
            "pay_site_flag": r.pay_site_flag,
            "inactive_date": r.inactive_date,
            "last_update_date": r.last_update_date,
        }

    @staticmethod
    def _item_to_dict(r: MtlSystemItem) -> dict[str, Any]:
        return {
            "inventory_item_id": r.inventory_item_id,
            "organization_id": r.organization_id,
            "segment1": r.segment1,
            "description": r.description,
            "primary_uom_code": r.primary_uom_code,
            "item_type": r.item_type,
            "buyer_id": r.buyer_id,
            "list_price_per_unit": float(r.list_price_per_unit)
            if r.list_price_per_unit is not None
            else None,
            "purchasing_item_flag": r.purchasing_item_flag,
            "purchasing_enabled_flag": r.purchasing_enabled_flag,
            "inventory_item_status_code": r.inventory_item_status_code,
            "last_update_date": r.last_update_date,
        }
