"""Chroma 向量存储模块单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.knowledge.vector_store import (
    VectorStore,
    InitializationError,
    DocumentError,
    SearchError,
)


@pytest.fixture()
def vs() -> VectorStore:
    """创建未初始化的 VectorStore 实例。"""
    return VectorStore(persist_directory="/tmp/test_chroma", collection_name="test_col")


@pytest.fixture()
def initialized_vs(vs: VectorStore) -> VectorStore:
    """创建已初始化的 VectorStore（mock Chroma）。"""
    mock_collection = MagicMock()
    vs._client = MagicMock()
    vs._collection = mock_collection
    return vs


class TestVectorStoreInit:
    """初始化测试。"""

    def test_initialize_success(self) -> None:
        """初始化成功时应设置 client 和 collection。"""
        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection

        with patch("core.knowledge.vector_store.chromadb.PersistentClient", return_value=mock_client):
            vs = VectorStore(persist_directory="/tmp/test")
            vs.initialize()

        assert vs._client is mock_client
        assert vs._collection is mock_collection

    def test_initialize_failure(self) -> None:
        """初始化失败应抛出 InitializationError。"""
        with patch(
            "core.knowledge.vector_store.chromadb.PersistentClient",
            side_effect=Exception("chroma fail"),
        ):
            vs = VectorStore(persist_directory="/tmp/test")
            with pytest.raises(InitializationError, match="Chroma 初始化失败"):
                vs.initialize()
        assert vs._client is None
        assert vs._collection is None

    def test_ensure_collection_not_initialized(self, vs: VectorStore) -> None:
        """未初始化时 _ensure_collection 应抛出 InitializationError。"""
        with pytest.raises(InitializationError, match="尚未初始化"):
            vs._ensure_collection()

    def test_ensure_collection_initialized(self, initialized_vs: VectorStore) -> None:
        """已初始化时 _ensure_collection 应返回 collection。"""
        col = initialized_vs._ensure_collection()
        assert col is initialized_vs._collection


class TestAddDocuments:
    """文档添加测试。"""

    def test_add_documents_success(self, initialized_vs: VectorStore) -> None:
        """批量添加文档应调用 upsert。"""
        docs = [
            {"id": "doc1", "text": "hello", "metadata": {"source": "test"}},
            {"id": "doc2", "text": "world"},
        ]
        initialized_vs.add_documents(docs)
        initialized_vs._collection.upsert.assert_called_once()

    def test_add_documents_empty(self, initialized_vs: VectorStore) -> None:
        """空文档列表应直接返回。"""
        initialized_vs.add_documents([])
        initialized_vs._collection.upsert.assert_not_called()

    def test_add_documents_missing_fields(self, initialized_vs: VectorStore) -> None:
        """缺少必要字段应抛出 DocumentError。"""
        with pytest.raises(DocumentError, match="必须包含"):
            initialized_vs.add_documents([{"id": "doc1"}])

    def test_add_documents_missing_text(self, initialized_vs: VectorStore) -> None:
        """缺少 text 字段应抛出 DocumentError。"""
        with pytest.raises(DocumentError, match="必须包含"):
            initialized_vs.add_documents([{"text": "no id"}])

    def test_add_documents_upsert_error(self, initialized_vs: VectorStore) -> None:
        """upsert 失败应抛出 DocumentError。"""
        initialized_vs._collection.upsert.side_effect = RuntimeError("write fail")
        with pytest.raises(DocumentError, match="批量写入"):
            initialized_vs.add_documents([{"id": "d1", "text": "t1"}])


class TestSearch:
    """语义检索测试。"""

    def test_search_success(self, initialized_vs: VectorStore) -> None:
        """检索应返回格式化结果。"""
        initialized_vs._collection.query.return_value = {
            "ids": [["id1", "id2"]],
            "documents": [["doc1", "doc2"]],
            "metadatas": [[{"k": "v1"}, {"k": "v2"}]],
            "distances": [[0.1, 0.5]],
        }
        results = initialized_vs.search("test query", top_k=2)
        assert len(results) == 2
        assert results[0]["id"] == "id1"
        assert results[0]["text"] == "doc1"
        assert results[0]["distance"] == 0.1

    def test_search_empty_results(self, initialized_vs: VectorStore) -> None:
        """无结果时应返回空列表。"""
        initialized_vs._collection.query.return_value = {"ids": []}
        results = initialized_vs.search("nothing")
        assert results == []

    def test_search_none_results(self, initialized_vs: VectorStore) -> None:
        """返回 None 时应返回空列表。"""
        initialized_vs._collection.query.return_value = None
        results = initialized_vs.search("nothing")
        assert results == []

    def test_search_partial_results(self, initialized_vs: VectorStore) -> None:
        """部分字段缺失时应使用默认值。"""
        initialized_vs._collection.query.return_value = {
            "ids": [["id1"]],
        }
        results = initialized_vs.search("test")
        assert len(results) == 1
        assert results[0]["text"] == ""
        assert results[0]["metadata"] == {}
        assert results[0]["distance"] == 0.0

    def test_search_error(self, initialized_vs: VectorStore) -> None:
        """检索失败应抛出 SearchError。"""
        initialized_vs._collection.query.side_effect = RuntimeError("search fail")
        with pytest.raises(SearchError, match="语义检索失败"):
            initialized_vs.search("test")


class TestOntologyContext:
    """本体上下文注入测试。"""

    def test_add_ontology_context(self, initialized_vs: VectorStore) -> None:
        """注入本体上下文应调用 upsert。"""
        rules = {
            "RULE_001": {
                "name": "三路匹配",
                "description": "金额偏差检测",
                "category": "three_way_match",
            }
        }
        entities = ["PurchaseOrder（采购订单，对应 PO_HEADERS_ALL）"]
        initialized_vs.add_ontology_context(rules=rules, entities=entities)
        initialized_vs._collection.upsert.assert_called_once()

        call_args = initialized_vs._collection.upsert.call_args
        assert len(call_args.kwargs["ids"]) == 2  # 1 rule + 1 entity

    def test_add_ontology_context_entity_without_bracket(self, initialized_vs: VectorStore) -> None:
        """实体描述不含括号时应使用完整文本作为名称。"""
        initialized_vs.add_ontology_context(rules={}, entities=["SimpleEntity"])
        initialized_vs._collection.upsert.assert_called_once()
        call_args = initialized_vs._collection.upsert.call_args
        assert "ontology_entity_SimpleEntity" in call_args.kwargs["ids"]
