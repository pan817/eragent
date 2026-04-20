"""Declarative mapping registry — 20 EBS tables → Graphiti nodes + edges.

Each entry defines how one EBS table maps to a node type, which columns
become node properties, which temporal fields to use, and what edges to
create from foreign-key relationships.
"""

from __future__ import annotations

from core.etl.models import EdgeMapping, TableMapping

MAPPING_REGISTRY: dict[str, TableMapping] = {
    # ── Master Data ───────────────────────────────────────────
    "AP_SUPPLIERS": TableMapping(
        node_type="Supplier",
        id_field="vendor_id",
        property_mapping={
            "vendor_name": "vendor_name",
            "segment1": "segment1",
            "vendor_type": "vendor_type_lookup_code",
            "terms_id": "terms_id",
            "enabled_flag": "enabled_flag",
            "industry_class": "standard_industry_class",
            "small_business_flag": "small_business_flag",
            "women_owned_flag": "women_owned_flag",
        },
        temporal={
            "valid_from": "start_date_active",
            "valid_to": "end_date_active",
            "last_updated": "last_update_date",
        },
        edges=[],
        free_text_fields=[],
    ),
    "AP_SUPPLIER_SITES_ALL": TableMapping(
        node_type="SupplierSite",
        id_field="vendor_site_id",
        property_mapping={
            "vendor_site_code": "vendor_site_code",
            "city": "city",
            "state": "state",
            "country": "country",
            "phone": "phone",
            "email": "email_address",
            "purchasing_site_flag": "purchasing_site_flag",
            "pay_site_flag": "pay_site_flag",
        },
        temporal={
            "valid_from": None,
            "valid_to": "inactive_date",
            "last_updated": "last_update_date",
        },
        edges=[
            EdgeMapping(
                edge_type="HAS_SITE",
                source=("Supplier", "vendor_id"),
                target=("SupplierSite", "vendor_site_id"),
            ),
        ],
    ),
    "MTL_SYSTEM_ITEMS_B": TableMapping(
        node_type="Material",
        id_field="inventory_item_id",
        property_mapping={
            "segment1": "segment1",
            "description": "description",
            "primary_uom_code": "primary_uom_code",
            "item_type": "item_type",
            "list_price_per_unit": ("list_price_per_unit", float),
            "purchasing_enabled_flag": "purchasing_enabled_flag",
            "status_code": "inventory_item_status_code",
        },
        temporal={
            "valid_from": None,
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[],
    ),
    # ── Purchasing ────────────────────────────────────────────
    "PO_HEADERS_ALL": TableMapping(
        node_type="PurchaseOrder",
        id_field="po_header_id",
        property_mapping={
            "po_number": "po_number",
            "type": "type_lookup_code",
            "status": "status",
            "authorization_status": "authorization_status",
            "total_amount": ("total_amount", float),
            "currency": "currency",
            "revision_num": "revision_num",
            "buyer_id": "buyer_id",
            "closed_code": "closed_code",
            "comments": "comments",
        },
        temporal={
            "valid_from": "creation_date",
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[
            EdgeMapping(
                edge_type="CREATES_PO",
                source=("Supplier", "vendor_id"),
                target=("PurchaseOrder", "po_header_id"),
                timestamp_field="creation_date",
            ),
        ],
        free_text_fields=["comments"],
    ),
    "PO_LINES_ALL": TableMapping(
        node_type="POLine",
        id_field="po_line_id",
        property_mapping={
            "line_num": "line_num",
            "item_id": "item_id",
            "item_description": "item_description",
            "quantity": "quantity",
            "unit_price": ("unit_price", float),
            "amount": ("amount", float),
            "category_id": "category_id",
            "standard_price": ("standard_price", float),
            "unit_meas": "unit_meas_lookup_code",
            "closed_code": "closed_code",
        },
        temporal={
            "valid_from": None,
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[
            EdgeMapping(
                edge_type="CONTAINS_LINE",
                source=("PurchaseOrder", "po_header_id"),
                target=("POLine", "po_line_id"),
            ),
            EdgeMapping(
                edge_type="ORDERS_MATERIAL",
                source=("POLine", "po_line_id"),
                target=("Material", "item_id"),
                condition=lambda row: bool(row.get("item_id")),
            ),
        ],
    ),
    "PO_LINE_LOCATIONS_ALL": TableMapping(
        node_type="POLine",  # enriches POLine, no separate node
        id_field="line_location_id",
        property_mapping={
            "promised_date": "promised_date",
            "need_by_date": "need_by_date",
            "shipment_num": "shipment_num",
            "quantity_received": ("quantity_received", float),
            "quantity_billed": ("quantity_billed", float),
            "inspection_required_flag": "inspection_required_flag",
        },
        temporal={
            "valid_from": None,
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[],
    ),
    "PO_DISTRIBUTIONS_ALL": TableMapping(
        node_type="POLine",  # enriches POLine, no separate node
        id_field="po_distribution_id",
        property_mapping={
            "quantity_ordered": ("quantity_ordered", float),
            "quantity_delivered": ("quantity_delivered", float),
            "quantity_billed": ("quantity_billed", float),
            "destination_type_code": "destination_type_code",
        },
        temporal={
            "valid_from": None,
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[],
    ),
    # ── Receiving ─────────────────────────────────────────────
    "RCV_SHIPMENT_HEADERS": TableMapping(
        node_type="Shipment",
        id_field="shipment_header_id",
        property_mapping={
            "receipt_num": "receipt_num",
            "shipped_date": "shipped_date",
            "expected_receipt_date": "expected_receipt_date",
            "receipt_source_code": "receipt_source_code",
            "freight_carrier_code": "freight_carrier_code",
            "comments": "comments",
        },
        temporal={
            "valid_from": "shipped_date",
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[
            EdgeMapping(
                edge_type="SHIPS_TO",
                source=("Shipment", "shipment_header_id"),
                target=("SupplierSite", "vendor_site_id"),
                condition=lambda row: bool(row.get("vendor_site_id")),
            ),
        ],
        free_text_fields=["comments"],
    ),
    "RCV_SHIPMENT_LINES": TableMapping(
        node_type="Shipment",  # enriches Shipment
        id_field="shipment_line_id",
        property_mapping={
            "item_id": "item_id",
            "quantity_shipped": ("quantity_shipped", float),
            "quantity_received": ("quantity_received", float),
            "unit_of_measure": "unit_of_measure",
        },
        temporal={
            "valid_from": None,
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[],
    ),
    "RCV_TRANSACTIONS": TableMapping(
        node_type="Receipt",
        id_field="transaction_id",
        property_mapping={
            "transaction_type": "transaction_type",
            "quantity": "quantity",
            "accepted_quantity": "accepted_quantity",
            "rejected_quantity": "rejected_quantity",
            "source_document_code": "source_document_code",
            "destination_type_code": "destination_type_code",
            "inspection_status_code": "inspection_status_code",
        },
        temporal={
            "valid_from": "transaction_date",
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[
            EdgeMapping(
                edge_type="BELONGS_TO_SHIPMENT",
                source=("Receipt", "transaction_id"),
                target=("Shipment", "shipment_header_id"),
            ),
            EdgeMapping(
                edge_type="RECEIVES_LINE",
                source=("Receipt", "transaction_id"),
                target=("POLine", "po_line_id"),
            ),
        ],
    ),
    # ── Payables ──────────────────────────────────────────────
    "AP_INVOICES_ALL": TableMapping(
        node_type="Invoice",
        id_field="invoice_id",
        property_mapping={
            "invoice_num": "invoice_num",
            "invoice_type": "invoice_type_lookup_code",
            "invoice_amount": ("invoice_amount", float),
            "amount_paid": ("amount_paid", float),
            "currency": "invoice_currency_code",
            "approval_status": "approval_status",
            "description": "description",
        },
        temporal={
            "valid_from": "invoice_date",
            "valid_to": "cancelled_date",
            "last_updated": "last_update_date",
        },
        edges=[
            EdgeMapping(
                edge_type="SUBMITS_INVOICE",
                source=("Supplier", "vendor_id"),
                target=("Invoice", "invoice_id"),
                timestamp_field="invoice_date",
            ),
        ],
        free_text_fields=["description"],
    ),
    "AP_INVOICE_LINES_ALL": TableMapping(
        node_type="InvoiceLine",
        id_field="invoice_line_id",
        property_mapping={
            "line_number": "line_number",
            "line_type": "line_type_lookup_code",
            "amount": ("amount", float),
            "quantity_invoiced": ("quantity_invoiced", float),
            "unit_price": ("unit_price", float),
            "item_description": "item_description",
        },
        temporal={
            "valid_from": "accounting_date",
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[
            EdgeMapping(
                edge_type="BELONGS_TO_INVOICE",
                source=("InvoiceLine", "invoice_line_id"),
                target=("Invoice", "invoice_id"),
            ),
            EdgeMapping(
                edge_type="INVOICES_LINE",
                source=("InvoiceLine", "invoice_line_id"),
                target=("POLine", "po_line_id"),
                condition=lambda row: bool(row.get("po_line_id")),
            ),
        ],
    ),
    "AP_INVOICE_DISTRIBUTIONS_ALL": TableMapping(
        node_type="InvoiceLine",  # enriches InvoiceLine
        id_field="invoice_distribution_id",
        property_mapping={
            "amount": ("amount", float),
            "match_status_flag": "match_status_flag",
            "posted_flag": "posted_flag",
        },
        temporal={
            "valid_from": "accounting_date",
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[],
    ),
    "AP_CHECKS_ALL": TableMapping(
        node_type="Payment",
        id_field="check_id",
        property_mapping={
            "check_number": "check_number",
            "amount": ("amount", float),
            "payment_method_code": "payment_method_code",
            "status_lookup_code": "status_lookup_code",
        },
        temporal={
            "valid_from": "check_date",
            "valid_to": "void_date",
            "last_updated": "last_update_date",
        },
        edges=[],
    ),
    "AP_INVOICE_PAYMENTS_ALL": TableMapping(
        node_type="Payment",  # creates edge, not a new node
        id_field="invoice_payment_id",
        property_mapping={
            "amount": ("amount", float),
            "discount_taken": ("discount_taken", float),
            "discount_lost": ("discount_lost", float),
        },
        temporal={
            "valid_from": "accounting_date",
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[
            EdgeMapping(
                edge_type="PAYS_INVOICE",
                source=("Payment", "check_id"),
                target=("Invoice", "invoice_id"),
                timestamp_field="accounting_date",
            ),
        ],
    ),
    "AP_PAYMENT_SCHEDULES_ALL": TableMapping(
        node_type="PaymentSchedule",
        id_field="payment_schedule_id",
        property_mapping={
            "payment_num": "payment_num",
            "gross_amount": ("gross_amount", float),
            "amount_remaining": ("amount_remaining", float),
            "payment_priority": "payment_priority",
            "hold_flag": "hold_flag",
            "payment_status_flag": "payment_status_flag",
        },
        temporal={
            "valid_from": None,
            "valid_to": "due_date",
            "last_updated": "last_update_date",
        },
        edges=[
            EdgeMapping(
                edge_type="SCHEDULED_FOR",
                source=("PaymentSchedule", "payment_schedule_id"),
                target=("Invoice", "invoice_id"),
            ),
        ],
    ),
    # ── Sourcing / Contracts ──────────────────────────────────
    "PON_AUCTION_HEADERS_ALL": TableMapping(
        node_type="Auction",
        id_field="auction_header_id",
        property_mapping={
            "document_number": "document_number",
            "auction_title": "auction_title",
            "auction_type": "auction_type",
            "auction_status": "auction_status",
            "outcome": "outcome",
            "contract_type": "contract_type",
        },
        temporal={
            "valid_from": "open_bidding_date",
            "valid_to": "close_bidding_date",
            "last_updated": "last_update_date",
        },
        edges=[],
    ),
    "PON_BID_HEADERS": TableMapping(
        node_type="Bid",
        id_field="bid_number",
        property_mapping={
            "bid_status": "bid_status",
            "bid_total": ("bid_total", float),
            "bid_currency_code": "bid_currency_code",
            "award_status": "award_status",
        },
        temporal={
            "valid_from": "publish_date",
            "valid_to": None,
            "last_updated": "last_update_date",
        },
        edges=[
            EdgeMapping(
                edge_type="HAS_BID",
                source=("Auction", "auction_header_id"),
                target=("Bid", "bid_number"),
            ),
            EdgeMapping(
                edge_type="BIDS_ON",
                source=("Supplier", "vendor_id"),
                target=("Auction", "auction_header_id"),
                condition=lambda row: bool(row.get("vendor_id")),
            ),
        ],
    ),
    "OKC_K_HEADERS_B": TableMapping(
        node_type="Contract",
        id_field="id",
        property_mapping={
            "contract_number": "contract_number",
            "status": "sts_code",
            "estimated_amount": ("estimated_amount", float),
            "currency": "currency_code",
            "buy_or_sell": "buy_or_sell",
            "category": "scs_code",
            "description": "description",
            "short_description": "short_description",
        },
        temporal={
            "valid_from": "start_date",
            "valid_to": "end_date",
            "last_updated": "last_update_date",
        },
        edges=[],
        free_text_fields=["description", "short_description"],
    ),
    "OKC_K_LINES_B": TableMapping(
        node_type="ContractLine",
        id_field="id",
        property_mapping={
            "line_number": "line_number",
            "status": "sts_code",
            "price_unit": ("price_unit", float),
            "price_negotiated": ("price_negotiated", float),
            "quantity": ("quantity", float),
            "uom_code": "uom_code",
            "item_description": "item_description",
        },
        temporal={
            "valid_from": "start_date",
            "valid_to": "end_date",
            "last_updated": "last_update_date",
        },
        edges=[
            EdgeMapping(
                edge_type="CONTAINS_CONTRACT_LINE",
                source=("Contract", "chr_id"),
                target=("ContractLine", "id"),
            ),
            EdgeMapping(
                edge_type="CONTRACT_COVERS",
                source=("ContractLine", "id"),
                target=("Material", "item_id"),
                condition=lambda row: bool(row.get("item_id")),
            ),
        ],
    ),
}
