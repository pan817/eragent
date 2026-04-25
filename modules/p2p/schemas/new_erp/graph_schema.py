"""New ERP graph schema — node/edge type mappings for Neo4j Cypher queries."""

from modules.p2p.schemas.protocol import GraphSchema

NEW_ERP_GRAPH_SCHEMA = GraphSchema(
    node_types={
        "purchase_order": "NewPurchaseOrder",
        "supplier": "NewSupplier",
        "invoice": "NewInvoice",
        "payment": "NewPayment",
        "receipt": "NewReceipt",
        "material": "NewMaterial",
        "po_line": "NewPOLine",
        "contract": "NewContract",
        "auction": "NewAuction",
        "supplier_site": "NewSupplierSite",
        "contract_line": "NewContractLine",
    },
    edge_types={
        "creates_po": "NEW_CREATES_PO",
        "contains_line": "NEW_CONTAINS_LINE",
        "receives_line": "NEW_RECEIVES_LINE",
        "invoices_line": "NEW_INVOICES_LINE",
        "belongs_to_invoice": "NEW_BELONGS_TO_INVOICE",
        "pays_invoice": "NEW_PAYS_INVOICE",
        "submits_invoice": "NEW_SUBMITS_INVOICE",
        "has_site": "NEW_HAS_SITE",
        "bids_on": "NEW_BIDS_ON",
        "has_bid": "NEW_HAS_BID",
        "orders_material": "NEW_ORDERS_MATERIAL",
        "contract_covers": "NEW_CONTRACT_COVERS",
        "contains_contract_line": "NEW_CONTAINS_CONTRACT_LINE",
    },
    business_id_fields={
        "NewPurchaseOrder": ["po_number"],
        "NewSupplier": ["vendor_id", "segment1"],
        "NewInvoice": ["invoice_num"],
        "NewPayment": ["check_number"],
        "NewReceipt": ["receipt_num"],
        "NewMaterial": ["segment1"],
        "NewContract": ["contract_number"],
        "NewAuction": ["document_number"],
        "NewSupplierSite": ["vendor_site_code"],
        "NewPOLine": ["po_number"],
    },
)
