"""Neo4j 知识图谱模块单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from core.knowledge.graph import (
    KnowledgeGraph,
    ConnectionError,
    NodeCreationError,
    RelationshipError,
    QueryError,
    SchemaError,
)


@pytest.fixture()
def kg() -> KnowledgeGraph:
    """创建未连接的 KnowledgeGraph 实例。"""
    return KnowledgeGraph(
        uri="bolt://localhost:7687",
        username="neo4j",
        password="test",
        database="testdb",
    )


class TestKnowledgeGraphConnect:
    """连接管理测试。"""

    def test_connect_success(self, kg: KnowledgeGraph) -> None:
        """连接成功时应设置 _driver。"""
        mock_driver = MagicMock()
        with patch("core.knowledge.graph.GraphDatabase.driver", return_value=mock_driver):
            kg.connect()
        assert kg._driver is mock_driver
        mock_driver.verify_connectivity.assert_called_once()

    def test_connect_failure(self, kg: KnowledgeGraph) -> None:
        """连接失败时应抛出 ConnectionError。"""
        with patch("core.knowledge.graph.GraphDatabase.driver", side_effect=Exception("refused")):
            with pytest.raises(ConnectionError, match="无法连接到 Neo4j"):
                kg.connect()
        assert kg._driver is None

    def test_close_with_driver(self, kg: KnowledgeGraph) -> None:
        """关闭时应调用 driver.close()。"""
        mock_driver = MagicMock()
        kg._driver = mock_driver
        kg.close()
        mock_driver.close.assert_called_once()
        assert kg._driver is None

    def test_close_without_driver(self, kg: KnowledgeGraph) -> None:
        """无连接时关闭应静默返回。"""
        kg.close()  # 不抛异常

    def test_close_with_exception(self, kg: KnowledgeGraph) -> None:
        """driver.close() 异常时应静默处理。"""
        mock_driver = MagicMock()
        mock_driver.close.side_effect = Exception("close failed")
        kg._driver = mock_driver
        kg.close()
        assert kg._driver is None


class TestKnowledgeGraphSession:
    """会话管理测试。"""

    def test_get_session_no_driver(self, kg: KnowledgeGraph) -> None:
        """未连接时获取 session 应抛出 ConnectionError。"""
        with pytest.raises(ConnectionError, match="驱动未初始化"):
            kg._get_session()

    def test_get_session_with_driver(self, kg: KnowledgeGraph) -> None:
        """已连接时应返回 session。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver
        session = kg._get_session()
        assert session is mock_session
        mock_driver.session.assert_called_once_with(database="testdb")


class TestNodeCreation:
    """节点创建测试。"""

    def _setup_kg_with_session(self, kg: KnowledgeGraph) -> MagicMock:
        """设置带有 mock session 的 KnowledgeGraph。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_record = {"node_id": "test-id"}
        mock_result.single.return_value = mock_record
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver
        return mock_session

    def test_create_supplier_node(self, kg: KnowledgeGraph) -> None:
        """创建供应商节点应返回 supplier_id。"""
        self._setup_kg_with_session(kg)
        result = kg.create_supplier_node({"supplier_id": "SUP-001", "name": "Test"})
        assert result == "test-id"

    def test_create_po_node(self, kg: KnowledgeGraph) -> None:
        """创建 PO 节点应返回 po_number。"""
        self._setup_kg_with_session(kg)
        result = kg.create_po_node({"po_number": "PO-001", "amount": 1000})
        assert result == "test-id"

    def test_create_invoice_node(self, kg: KnowledgeGraph) -> None:
        """创建发票节点应返回 invoice_id。"""
        self._setup_kg_with_session(kg)
        result = kg.create_invoice_node({"invoice_id": "INV-001"})
        assert result == "test-id"

    def test_create_receipt_node(self, kg: KnowledgeGraph) -> None:
        """创建收货节点应返回 receipt_id。"""
        self._setup_kg_with_session(kg)
        result = kg.create_receipt_node({"receipt_id": "RCV-001"})
        assert result == "test-id"

    def test_create_payment_node(self, kg: KnowledgeGraph) -> None:
        """创建付款节点应返回 payment_id。"""
        self._setup_kg_with_session(kg)
        result = kg.create_payment_node({"payment_id": "PAY-001"})
        assert result == "test-id"

    def test_create_node_missing_id_key(self, kg: KnowledgeGraph) -> None:
        """缺少 ID 字段应抛出 NodeCreationError。"""
        kg._driver = MagicMock()
        with pytest.raises(NodeCreationError, match="缺少必填字段"):
            kg.create_supplier_node({"name": "no id"})

    def test_create_node_db_error(self, kg: KnowledgeGraph) -> None:
        """数据库写入失败应抛出 NodeCreationError。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = RuntimeError("db error")
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver
        with pytest.raises(NodeCreationError, match="创建 Supplier 节点失败"):
            kg.create_supplier_node({"supplier_id": "SUP-001"})

    def test_create_node_no_record(self, kg: KnowledgeGraph) -> None:
        """single() 返回 None 时应使用 data 中的 id。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.single.return_value = None
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver
        result = kg.create_supplier_node({"supplier_id": "SUP-002"})
        assert result == "SUP-002"


class TestRelationship:
    """关系创建测试。"""

    def test_create_relationship_success(self, kg: KnowledgeGraph) -> None:
        """创建关系应正常执行。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_summary = MagicMock()
        mock_summary.counters.relationships_created = 1
        mock_result.consume.return_value = mock_summary
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver

        kg.create_relationship("SUP-001", "PO-001", "ISSUED_BY")

    def test_create_relationship_no_creation(self, kg: KnowledgeGraph) -> None:
        """MERGE 未创建关系时应静默通过。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_summary = MagicMock()
        mock_summary.counters.relationships_created = 0
        mock_result.consume.return_value = mock_summary
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver

        kg.create_relationship("SUP-001", "PO-001", "ISSUED_BY")

    def test_create_relationship_with_props(self, kg: KnowledgeGraph) -> None:
        """带属性的关系创建。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_summary = MagicMock()
        mock_summary.counters.relationships_created = 0
        mock_result.consume.return_value = mock_summary
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver

        kg.create_relationship("A", "B", "REL", props={"weight": 1.0})

    def test_create_relationship_error(self, kg: KnowledgeGraph) -> None:
        """关系创建失败应抛出 RelationshipError。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = RuntimeError("fail")
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver

        with pytest.raises(RelationshipError):
            kg.create_relationship("A", "B", "REL")


class TestQueries:
    """查询测试。"""

    def _setup_query(self, kg: KnowledgeGraph, records: list) -> None:
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter(records))
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver

    def test_query_supplier_pos(self, kg: KnowledgeGraph) -> None:
        """查询供应商 PO 应返回列表。"""
        record = MagicMock()
        record.__getitem__ = MagicMock(return_value={"po_number": "PO-001"})
        self._setup_query(kg, [record])
        result = kg.query_supplier_pos("SUP-001")
        assert len(result) == 1

    def test_query_supplier_pos_empty(self, kg: KnowledgeGraph) -> None:
        """无 PO 时应返回空列表。"""
        self._setup_query(kg, [])
        result = kg.query_supplier_pos("SUP-999")
        assert result == []

    def test_query_supplier_pos_error(self, kg: KnowledgeGraph) -> None:
        """查询失败应抛出 QueryError。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = RuntimeError("query fail")
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver
        with pytest.raises(QueryError):
            kg.query_supplier_pos("SUP-001")

    def test_query_po_invoices(self, kg: KnowledgeGraph) -> None:
        """查询 PO 发票应返回列表。"""
        record = MagicMock()
        record.__getitem__ = MagicMock(return_value={"invoice_id": "INV-001"})
        self._setup_query(kg, [record])
        result = kg.query_po_invoices("PO-001")
        assert len(result) == 1

    def test_query_po_invoices_error(self, kg: KnowledgeGraph) -> None:
        """查询失败应抛出 QueryError。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = RuntimeError("fail")
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver
        with pytest.raises(QueryError):
            kg.query_po_invoices("PO-001")

    def test_query_supplier_payments(self, kg: KnowledgeGraph) -> None:
        """查询供应商付款应返回列表。"""
        record = MagicMock()
        record.__getitem__ = MagicMock(return_value={"payment_id": "PAY-001"})
        self._setup_query(kg, [record])
        result = kg.query_supplier_payments("SUP-001")
        assert len(result) == 1

    def test_query_supplier_payments_error(self, kg: KnowledgeGraph) -> None:
        """查询失败应抛出 QueryError。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = RuntimeError("fail")
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver
        with pytest.raises(QueryError):
            kg.query_supplier_payments("SUP-001")


class TestSyncOntologySchema:
    """本体 Schema 同步测试。"""

    def test_sync_success(self, kg: KnowledgeGraph) -> None:
        """同步 Schema 应执行约束和索引创建。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver

        kg.sync_ontology_schema(
            classes=["Supplier", "PurchaseOrder"],
            relationships=["ISSUED_BY"],
        )
        assert mock_session.run.call_count == 3  # 2 constraints + 1 index

    def test_sync_unknown_class(self, kg: KnowledgeGraph) -> None:
        """未知类名应使用默认 id 字段。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver

        kg.sync_ontology_schema(classes=["UnknownClass"], relationships=[])
        call_args = mock_session.run.call_args[0][0]
        assert "n.id IS UNIQUE" in call_args

    def test_sync_error(self, kg: KnowledgeGraph) -> None:
        """同步失败应抛出 SchemaError。"""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = RuntimeError("schema fail")
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        kg._driver = mock_driver

        with pytest.raises(SchemaError, match="同步本体 Schema"):
            kg.sync_ontology_schema(classes=["Supplier"], relationships=[])
