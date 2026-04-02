"""
Chroma 向量存储模块。

封装 Chroma 向量数据库的初始化、文档写入和语义检索，用于：
- P2P 业务文档的向量化存储与检索
- OWL 本体规则和实体定义的向量化注入（增强 RAG 上下文）

MVP 阶段使用 Chroma 内置的默认 Embedding 模型，不依赖外部 Embedding API。
"""

from __future__ import annotations

from typing import Any

import chromadb  # type: ignore[import-untyped]
from chromadb.api import ClientAPI  # type: ignore[import-untyped]
from chromadb.api.models.Collection import Collection  # type: ignore[import-untyped]


class VectorStoreError(Exception):
    """向量存储操作异常基类。"""


class InitializationError(VectorStoreError):
    """Chroma 初始化失败异常。"""


class DocumentError(VectorStoreError):
    """文档操作失败异常。"""


class SearchError(VectorStoreError):
    """检索失败异常。"""


class VectorStore:
    """
    Chroma 向量存储封装类。

    提供面向 P2P 业务场景的文档写入和语义检索能力，
    支持将本体规则和实体定义注入向量库以增强 RAG 检索效果。

    MVP 阶段使用 Chroma 内置的默认 Embedding 函数（无需外部 API）。
    """

    def __init__(
        self,
        persist_directory: str,
        collection_name: str = "erp_ontology",
    ) -> None:
        """
        初始化向量存储实例。

        Args:
            persist_directory: Chroma 持久化目录路径。
            collection_name: 集合名称，默认 ``erp_ontology``。
        """
        self._persist_directory = persist_directory
        self._collection_name = collection_name
        self._client: ClientAPI | None = None
        self._collection: Collection | None = None

    def initialize(self) -> None:
        """
        初始化 Chroma 客户端并获取或创建 Collection。

        使用 Chroma 的默认 Embedding 函数（all-MiniLM-L6-v2），
        无需外部 Embedding API。

        Raises:
            InitializationError: Chroma 客户端或 Collection 创建失败时抛出。
        """
        try:
            self._client = chromadb.PersistentClient(
                path=self._persist_directory,
            )
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as exc:
            self._client = None
            self._collection = None
            raise InitializationError(
                f"Chroma 初始化失败 (目录={self._persist_directory}): {exc}"
            ) from exc

    def _ensure_collection(self) -> Collection:
        """
        确保 Collection 已就绪。

        Returns:
            当前活跃的 Chroma Collection。

        Raises:
            InitializationError: Collection 未初始化时抛出。
        """
        if self._collection is None:
            raise InitializationError(
                "VectorStore 尚未初始化，请先调用 initialize()"
            )
        return self._collection

    # ------------------------------------------------------------------
    # 文档管理
    # ------------------------------------------------------------------

    def add_documents(self, docs: list[dict[str, Any]]) -> None:
        """
        批量添加文档到向量存储。

        文档以 UPSERT 语义写入，已有相同 ID 的文档将被覆盖。

        Args:
            docs: 文档列表，每个文档为包含以下键的字典：
                  - ``id`` (str): 文档唯一标识。
                  - ``text`` (str): 文档文本内容，将被向量化。
                  - ``metadata`` (dict, 可选): 附加元数据，用于过滤检索。

        Raises:
            DocumentError: 文档格式错误或写入失败时抛出。
        """
        collection = self._ensure_collection()

        if not docs:
            return

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []

        for doc in docs:
            if "id" not in doc or "text" not in doc:
                raise DocumentError(
                    "文档必须包含 'id' 和 'text' 字段，"
                    f"实际收到的键: {list(doc.keys())}"
                )
            ids.append(str(doc["id"]))
            documents.append(str(doc["text"]))
            metadatas.append(doc.get("metadata", {}))

        try:
            collection.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
            )
        except Exception as exc:
            raise DocumentError(
                f"批量写入 {len(docs)} 条文档失败: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # 语义检索
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """
        语义检索文档。

        使用 Chroma 内置 Embedding 将查询文本向量化后进行相似度检索。

        Args:
            query: 自然语言查询文本。
            top_k: 返回最相似的前 K 条结果，默认 5。

        Returns:
            检索结果列表，每条结果为包含以下键的字典：
            - ``id`` (str): 文档 ID。
            - ``text`` (str): 文档文本内容。
            - ``metadata`` (dict): 文档元数据。
            - ``distance`` (float): 与查询向量的距离（越小越相似）。

        Raises:
            SearchError: 检索执行失败时抛出。
        """
        collection = self._ensure_collection()

        try:
            results = collection.query(
                query_texts=[query],
                n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            raise SearchError(
                f"语义检索失败 (query='{query[:50]}...'): {exc}"
            ) from exc

        # 将 Chroma 返回格式转为扁平字典列表
        output: list[dict[str, Any]] = []
        if not results or not results.get("ids"):
            return output

        result_ids: list[str] = results["ids"][0]
        result_docs: list[str] = results["documents"][0] if results.get("documents") else [""] * len(result_ids)
        result_metas: list[dict[str, Any]] = results["metadatas"][0] if results.get("metadatas") else [{}] * len(result_ids)
        result_dists: list[float] = results["distances"][0] if results.get("distances") else [0.0] * len(result_ids)

        for doc_id, text, metadata, distance in zip(
            result_ids, result_docs, result_metas, result_dists
        ):
            output.append({
                "id": doc_id,
                "text": text,
                "metadata": metadata,
                "distance": distance,
            })

        return output

    # ------------------------------------------------------------------
    # 本体上下文注入
    # ------------------------------------------------------------------

    def add_ontology_context(
        self,
        rules: dict[str, dict[str, str]],
        entities: list[str],
    ) -> None:
        """
        将 OWL 本体规则和实体定义注入向量库。

        将每条合规规则和每个实体定义分别作为独立文档写入，
        通过 metadata 中的 ``source`` 字段标记来源为本体，
        便于检索时区分业务文档和本体知识。

        Args:
            rules: P2P 合规规则字典，格式与
                   ``core.ontology.reasoner.P2P_RULES`` 一致。
                   键为规则 ID，值为包含 name、description、category 等字段的字典。
            entities: 本体实体描述列表，如
                      ``["PurchaseOrder（采购订单，对应 PO_HEADERS_ALL）"]``。

        Raises:
            DocumentError: 写入失败时抛出。
        """
        docs: list[dict[str, Any]] = []

        # 将每条规则转为一条向量文档
        for rule_id, rule_meta in rules.items():
            rule_text = (
                f"合规规则: {rule_meta.get('name', rule_id)}\n"
                f"规则ID: {rule_id}\n"
                f"描述: {rule_meta.get('description', '')}\n"
                f"分类: {rule_meta.get('category', '')}"
            )
            docs.append({
                "id": f"ontology_rule_{rule_id}",
                "text": rule_text,
                "metadata": {
                    "source": "ontology",
                    "type": "rule",
                    "rule_id": rule_id,
                    "category": rule_meta.get("category", ""),
                },
            })

        # 将每个实体定义转为一条向量文档
        for idx, entity_desc in enumerate(entities):
            entity_name = entity_desc.split("（")[0].strip() if "（" in entity_desc else entity_desc
            docs.append({
                "id": f"ontology_entity_{entity_name}",
                "text": f"P2P 业务实体: {entity_desc}",
                "metadata": {
                    "source": "ontology",
                    "type": "entity",
                    "entity_name": entity_name,
                },
            })

        self.add_documents(docs)
