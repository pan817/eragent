"""
Neo4j 知识图谱操作模块。

封装 Neo4j 图数据库的连接管理和 CRUD 操作，提供：
- 供应商、采购订单、发票、收货、付款等 P2P 核心实体的节点创建
- 实体间关系的建立与查询
- OWL 本体 Schema 到 Neo4j 约束的同步

使用 neo4j Python driver 进行数据库交互。
"""

from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase, Driver, Session  # type: ignore[import-untyped]


class KnowledgeGraphError(Exception):
    """知识图谱操作异常基类。"""


class ConnectionError(KnowledgeGraphError):
    """Neo4j 连接失败异常。"""


class NodeCreationError(KnowledgeGraphError):
    """节点创建失败异常。"""


class RelationshipError(KnowledgeGraphError):
    """关系操作失败异常。"""


class QueryError(KnowledgeGraphError):
    """查询执行失败异常。"""


class SchemaError(KnowledgeGraphError):
    """Schema 同步失败异常。"""


class KnowledgeGraph:
    """
    Neo4j 知识图谱封装类。

    管理 P2P 业务领域的实体节点和关系，支持供应商、采购订单、
    发票、收货事务、付款等核心实体的 CRUD 操作，并可将 OWL 本体
    Schema 同步为 Neo4j 数据库约束。
    """

    def __init__(
        self,
        uri: str,
        username: str,
        password: str,
        database: str = "neo4j",
    ) -> None:
        """
        初始化知识图谱实例。

        Args:
            uri: Neo4j 连接地址，如 ``bolt://localhost:7687``。
            username: 数据库用户名。
            password: 数据库密码。
            database: 目标数据库名称，默认 ``neo4j``。
        """
        self._uri = uri
        self._username = username
        self._password = password
        self._database = database
        self._driver: Driver | None = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        建立到 Neo4j 的连接。

        Raises:
            ConnectionError: 连接失败时抛出。
        """
        try:
            self._driver = GraphDatabase.driver(
                self._uri,
                auth=(self._username, self._password),
            )
            # 验证连接可用
            self._driver.verify_connectivity()
        except Exception as exc:
            self._driver = None
            raise ConnectionError(
                f"无法连接到 Neo4j ({self._uri}): {exc}"
            ) from exc

    def close(self) -> None:
        """
        关闭 Neo4j 连接并释放资源。

        如果当前没有活跃连接则静默返回。
        """
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
            finally:
                self._driver = None

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _get_session(self) -> Session:
        """
        获取数据库会话。

        Returns:
            Neo4j Session 实例。

        Raises:
            ConnectionError: 驱动未初始化时抛出。
        """
        if self._driver is None:
            raise ConnectionError("Neo4j 驱动未初始化，请先调用 connect()")
        return self._driver.session(database=self._database)

    def _create_node(self, label: str, data: dict[str, Any], id_key: str) -> str:
        """
        创建单个节点的通用方法。

        Args:
            label: 节点标签（如 ``Supplier``、``PurchaseOrder``）。
            data: 节点属性字典，必须包含 *id_key* 对应的键。
            id_key: 用作业务主键的属性名。

        Returns:
            创建节点的业务 ID 值。

        Raises:
            NodeCreationError: 缺少必要字段或写入失败时抛出。
        """
        if id_key not in data:
            raise NodeCreationError(
                f"创建 {label} 节点失败：data 中缺少必填字段 '{id_key}'"
            )

        query = (
            f"MERGE (n:{label} {{{id_key}: $id_value}}) "
            f"SET n += $props "
            f"RETURN n.{id_key} AS node_id"
        )
        try:
            with self._get_session() as session:
                result = session.run(
                    query,
                    id_value=data[id_key],
                    props=data,
                )
                record = result.single()
                return str(record["node_id"]) if record else str(data[id_key])
        except KnowledgeGraphError:
            raise
        except Exception as exc:
            raise NodeCreationError(
                f"创建 {label} 节点失败: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # 节点创建
    # ------------------------------------------------------------------

    def create_supplier_node(self, data: dict[str, Any]) -> str:
        """
        创建或合并供应商节点。

        Args:
            data: 供应商属性字典，必须包含 ``supplier_id``。
                  典型字段: supplier_id, supplier_name, status, creation_date 等。

        Returns:
            供应商业务 ID（supplier_id 值）。

        Raises:
            NodeCreationError: 缺少 supplier_id 或写入失败时抛出。
        """
        return self._create_node("Supplier", data, "supplier_id")

    def create_po_node(self, data: dict[str, Any]) -> str:
        """
        创建或合并采购订单节点。

        Args:
            data: 采购订单属性字典，必须包含 ``po_number``。
                  典型字段: po_number, supplier_id, amount, currency,
                  creation_date, status 等。

        Returns:
            采购订单编号（po_number 值）。

        Raises:
            NodeCreationError: 缺少 po_number 或写入失败时抛出。
        """
        return self._create_node("PurchaseOrder", data, "po_number")

    def create_invoice_node(self, data: dict[str, Any]) -> str:
        """
        创建或合并发票节点。

        Args:
            data: 发票属性字典，必须包含 ``invoice_id``。
                  典型字段: invoice_id, invoice_number, po_number, amount,
                  due_date, status 等。

        Returns:
            发票业务 ID（invoice_id 值）。

        Raises:
            NodeCreationError: 缺少 invoice_id 或写入失败时抛出。
        """
        return self._create_node("Invoice", data, "invoice_id")

    def create_receipt_node(self, data: dict[str, Any]) -> str:
        """
        创建或合并收货事务节点。

        Args:
            data: 收货事务属性字典，必须包含 ``receipt_id``。
                  典型字段: receipt_id, po_number, quantity, receipt_date,
                  status 等。

        Returns:
            收货事务 ID（receipt_id 值）。

        Raises:
            NodeCreationError: 缺少 receipt_id 或写入失败时抛出。
        """
        return self._create_node("ReceiptTransaction", data, "receipt_id")

    def create_payment_node(self, data: dict[str, Any]) -> str:
        """
        创建或合并付款记录节点。

        Args:
            data: 付款属性字典，必须包含 ``payment_id``。
                  典型字段: payment_id, invoice_id, amount, payment_date,
                  payment_method 等。

        Returns:
            付款记录 ID（payment_id 值）。

        Raises:
            NodeCreationError: 缺少 payment_id 或写入失败时抛出。
        """
        return self._create_node("Payment", data, "payment_id")

    # ------------------------------------------------------------------
    # 关系创建
    # ------------------------------------------------------------------

    def create_relationship(
        self,
        from_id: str,
        to_id: str,
        rel_type: str,
        props: dict[str, Any] | None = None,
    ) -> None:
        """
        在两个节点之间创建关系。

        通过节点的任意唯一属性匹配起止节点，然后创建指定类型的关系。
        关系使用 MERGE 语义（幂等），不会重复创建。

        Args:
            from_id: 起始节点的业务 ID 值。
            to_id: 目标节点的业务 ID 值。
            rel_type: 关系类型，如 ``ISSUED_BY``、``HAS_INVOICE``、
                      ``APPLIED_TO_INVOICE`` 等。
            props: 关系上的附加属性，可选。

        Raises:
            RelationshipError: 节点不存在或写入失败时抛出。
        """
        props = props or {}
        query = (
            "MATCH (a) WHERE any(key IN keys(a) WHERE a[key] = $from_id) "
            "MATCH (b) WHERE any(key IN keys(b) WHERE b[key] = $to_id) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            "SET r += $props"
        )
        try:
            with self._get_session() as session:
                result = session.run(
                    query,
                    from_id=from_id,
                    to_id=to_id,
                    props=props,
                )
                summary = result.consume()
                if summary.counters.relationships_created == 0 and not props:
                    # MERGE 未创建也未更新，可能是节点不存在
                    pass
        except KnowledgeGraphError:
            raise
        except Exception as exc:
            raise RelationshipError(
                f"创建关系 ({from_id})-[:{rel_type}]->({to_id}) 失败: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query_supplier_pos(self, supplier_id: str) -> list[dict[str, Any]]:
        """
        查询指定供应商的所有采购订单。

        Args:
            supplier_id: 供应商业务 ID。

        Returns:
            采购订单属性字典列表；供应商不存在或无关联 PO 时返回空列表。

        Raises:
            QueryError: 查询执行异常时抛出。
        """
        query = (
            "MATCH (s:Supplier {supplier_id: $supplier_id})"
            "<-[:ISSUED_BY]-(po:PurchaseOrder) "
            "RETURN properties(po) AS po_data"
        )
        try:
            with self._get_session() as session:
                result = session.run(query, supplier_id=supplier_id)
                return [dict(record["po_data"]) for record in result]
        except Exception as exc:
            raise QueryError(
                f"查询供应商 {supplier_id} 的采购订单失败: {exc}"
            ) from exc

    def query_po_invoices(self, po_number: str) -> list[dict[str, Any]]:
        """
        查询指定采购订单关联的所有发票。

        Args:
            po_number: 采购订单编号。

        Returns:
            发票属性字典列表；PO 不存在或无关联发票时返回空列表。

        Raises:
            QueryError: 查询执行异常时抛出。
        """
        query = (
            "MATCH (po:PurchaseOrder {po_number: $po_number})"
            "<-[:REFERENCES_PO]-(inv:Invoice) "
            "RETURN properties(inv) AS invoice_data"
        )
        try:
            with self._get_session() as session:
                result = session.run(query, po_number=po_number)
                return [dict(record["invoice_data"]) for record in result]
        except Exception as exc:
            raise QueryError(
                f"查询采购订单 {po_number} 的发票失败: {exc}"
            ) from exc

    def query_supplier_payments(self, supplier_id: str) -> list[dict[str, Any]]:
        """
        查询指定供应商的所有付款记录。

        通过 Supplier -> PurchaseOrder -> Invoice -> Payment 路径查询。

        Args:
            supplier_id: 供应商业务 ID。

        Returns:
            付款记录属性字典列表；无匹配时返回空列表。

        Raises:
            QueryError: 查询执行异常时抛出。
        """
        query = (
            "MATCH (s:Supplier {supplier_id: $supplier_id})"
            "<-[:ISSUED_BY]-(po:PurchaseOrder)"
            "<-[:REFERENCES_PO]-(inv:Invoice)"
            "<-[:APPLIED_TO_INVOICE]-(pmt:Payment) "
            "RETURN properties(pmt) AS payment_data"
        )
        try:
            with self._get_session() as session:
                result = session.run(query, supplier_id=supplier_id)
                return [dict(record["payment_data"]) for record in result]
        except Exception as exc:
            raise QueryError(
                f"查询供应商 {supplier_id} 的付款记录失败: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Schema 同步
    # ------------------------------------------------------------------

    def sync_ontology_schema(
        self,
        classes: list[str],
        relationships: list[str],
    ) -> None:
        """
        将 OWL 本体的 Schema 信息同步到 Neo4j 约束。

        为本体中定义的每个类创建节点唯一性约束，为每个关系类型创建
        索引，确保图数据库 Schema 与本体定义保持一致。

        约束命名规则：
        - 节点约束: ``uniq_{class_name_lower}_id``
        - 关系索引: ``idx_rel_{rel_type_lower}``

        Args:
            classes: OWL 本体中定义的类名列表，如
                     ``["Supplier", "PurchaseOrder", "Invoice"]``。
            relationships: OWL 本体中定义的关系类型列表，如
                          ``["ISSUED_BY", "REFERENCES_PO"]``。

        Raises:
            SchemaError: 约束或索引创建失败时抛出。
        """
        # 类名到业务 ID 字段的映射
        id_field_mapping: dict[str, str] = {
            "Supplier": "supplier_id",
            "PurchaseOrder": "po_number",
            "PurchaseOrderLine": "po_line_id",
            "Invoice": "invoice_id",
            "ReceiptTransaction": "receipt_id",
            "Payment": "payment_id",
        }

        try:
            with self._get_session() as session:
                # 为每个本体类创建唯一性约束
                for cls_name in classes:
                    id_field = id_field_mapping.get(cls_name, "id")
                    constraint_name = f"uniq_{cls_name.lower()}_id"
                    cypher = (
                        f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                        f"FOR (n:{cls_name}) REQUIRE n.{id_field} IS UNIQUE"
                    )
                    session.run(cypher)

                # 为每个关系类型创建索引（提升遍历性能）
                for rel_type in relationships:
                    index_name = f"idx_rel_{rel_type.lower()}"
                    cypher = (
                        f"CREATE INDEX {index_name} IF NOT EXISTS "
                        f"FOR ()-[r:{rel_type}]-() ON (r.created_at)"
                    )
                    session.run(cypher)

        except KnowledgeGraphError:
            raise
        except Exception as exc:
            raise SchemaError(
                f"同步本体 Schema 到 Neo4j 失败: {exc}"
            ) from exc
