"""Oracle EBS graph schema — node/edge type mappings for Neo4j Cypher queries."""

from modules.p2p.schemas.protocol import GraphSchema

ORACLE_EBS_GRAPH_SCHEMA = GraphSchema(
    node_types={
        "purchase_order": "PurchaseOrder",
        "supplier": "Supplier",
        "invoice": "Invoice",
        "payment": "Payment",
        "receipt": "Receipt",
        "material": "Material",
        "po_line": "POLine",
        "contract": "Contract",
        "auction": "Auction",
        "supplier_site": "SupplierSite",
        "contract_line": "ContractLine",
    },
    edge_types={
        "creates_po": "CREATES_PO",
        "contains_line": "CONTAINS_LINE",
        "receives_line": "RECEIVES_LINE",
        "invoices_line": "INVOICES_LINE",
        "belongs_to_invoice": "BELONGS_TO_INVOICE",
        "pays_invoice": "PAYS_INVOICE",
        "submits_invoice": "SUBMITS_INVOICE",
        "has_site": "HAS_SITE",
        "bids_on": "BIDS_ON",
        "has_bid": "HAS_BID",
        "orders_material": "ORDERS_MATERIAL",
        "contract_covers": "CONTRACT_COVERS",
        "contains_contract_line": "CONTAINS_CONTRACT_LINE",
    },
    business_id_fields={
        "PurchaseOrder": ["po_number"],
        "Supplier": ["vendor_id", "segment1"],
        "Invoice": ["invoice_num"],
        "Payment": ["check_number"],
        "Receipt": ["receipt_num"],
        "Material": ["segment1"],
        "Contract": ["contract_number"],
        "Auction": ["document_number"],
        "SupplierSite": ["vendor_site_code"],
        "POLine": ["po_number"],
    },
)
