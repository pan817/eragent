"""Oracle EBS schema — auto-registers into SchemaRegistry on import."""

from modules.p2p.schemas.oracle_ebs.graph_schema import ORACLE_EBS_GRAPH_SCHEMA
from modules.p2p.schemas.oracle_ebs.repository import OracleEBSRepository


def _register() -> None:
    from modules.p2p.schemas import SchemaRegistration, SchemaRegistry

    SchemaRegistry.register(
        SchemaRegistration(
            name="oracle_ebs",
            repository_factory=OracleEBSRepository,
            graph_schema=ORACLE_EBS_GRAPH_SCHEMA,
        )
    )


_register()
