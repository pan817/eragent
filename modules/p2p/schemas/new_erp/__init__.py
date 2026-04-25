"""New ERP schema — auto-registers into SchemaRegistry on import."""

from modules.p2p.schemas.new_erp.graph_schema import NEW_ERP_GRAPH_SCHEMA
from modules.p2p.schemas.new_erp.repository import NewERPRepository


def _register() -> None:
    from modules.p2p.schemas import SchemaRegistration, SchemaRegistry

    SchemaRegistry.register(
        SchemaRegistration(
            name="new_erp",
            repository_factory=NewERPRepository,
            graph_schema=NEW_ERP_GRAPH_SCHEMA,
        )
    )


_register()
