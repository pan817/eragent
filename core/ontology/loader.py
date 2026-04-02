"""
OWL 本体加载模块。

使用 Owlready2 加载 P2P OWL 本体文件，提供类查询、实例创建、
属性访问等基础操作，作为推理层的数据基础。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import structlog

try:
    from owlready2 import get_ontology, onto_path, World
    OWLREADY2_AVAILABLE = True
except ImportError:
    OWLREADY2_AVAILABLE = False

logger = structlog.get_logger(__name__)

# P2P 本体 IRI
P2P_ONTOLOGY_IRI = "http://eragent.io/ontology/p2p"
# P2P 本体命名空间前缀
P2P_NAMESPACE = "http://eragent.io/ontology/p2p#"


class OntologyLoader:
    """
    OWL 本体加载器。

    负责从文件系统加载 OWL 本体，提供类和属性的查询接口。
    支持多本体文件加载，通过 Owlready2 World 管理本体隔离。
    """

    def __init__(self, owl_file_path: Path | str | None = None) -> None:
        """
        初始化本体加载器。

        Args:
            owl_file_path: OWL 文件路径，默认加载 P2P 本体文件。
        """
        self._owl_path = Path(owl_file_path) if owl_file_path else self._default_p2p_owl()
        self._world: Any | None = None
        self._ontology: Any | None = None
        self._loaded = False

    @staticmethod
    def _default_p2p_owl() -> Path:
        """返回默认的 P2P OWL 文件路径。"""
        return Path(__file__).parent.parent.parent / "modules" / "p2p" / "ontology" / "p2p.owl"

    def load(self) -> "OntologyLoader":
        """
        加载 OWL 本体文件。

        Returns:
            self，支持链式调用。

        Raises:
            FileNotFoundError: OWL 文件不存在时抛出。
            RuntimeError: owlready2 未安装或加载失败时抛出。
        """
        if not OWLREADY2_AVAILABLE:
            raise RuntimeError(
                "owlready2 未安装，请执行: pip install owlready2"
            )

        if not self._owl_path.exists():
            raise FileNotFoundError(f"OWL 本体文件不存在: {self._owl_path}")

        logger.info("加载 OWL 本体", path=str(self._owl_path))

        # 创建独立的 World 确保本体隔离
        self._world = World()

        # 将 OWL 文件所在目录添加到 onto_path，支持 owl:imports
        onto_dir = str(self._owl_path.parent)
        if onto_dir not in onto_path:
            onto_path.append(onto_dir)

        # 使用 file:// URI 加载本体
        file_uri = self._owl_path.as_uri()
        self._ontology = self._world.get_ontology(file_uri).load()
        self._loaded = True

        class_count = len(list(self._ontology.classes()))
        prop_count = len(list(self._ontology.properties()))
        logger.info(
            "本体加载完成",
            classes=class_count,
            properties=prop_count,
            iri=str(self._ontology.base_iri),
        )
        return self

    @property
    def ontology(self) -> Any:
        """获取已加载的本体对象。"""
        if not self._loaded or self._ontology is None:
            raise RuntimeError("本体尚未加载，请先调用 load() 方法")
        return self._ontology

    @property
    def world(self) -> Any:
        """获取 Owlready2 World 对象。"""
        if not self._loaded or self._world is None:
            raise RuntimeError("本体尚未加载，请先调用 load() 方法")
        return self._world

    @property
    def is_loaded(self) -> bool:
        """检查本体是否已成功加载。"""
        return self._loaded

    def get_class(self, class_name: str) -> Any | None:
        """
        按名称获取本体类。

        Args:
            class_name: 类的本地名称，如 'PurchaseOrder'。

        Returns:
            本体类对象，不存在时返回 None。
        """
        onto = self.ontology
        return onto[class_name]

    def get_all_classes(self) -> list[str]:
        """
        获取本体中所有类的名称列表。

        Returns:
            类名称列表。
        """
        return [cls.name for cls in self.ontology.classes()]

    def get_all_object_properties(self) -> list[str]:
        """
        获取本体中所有对象属性的名称列表。

        Returns:
            对象属性名称列表。
        """
        return [prop.name for prop in self.ontology.object_properties()]

    def get_all_data_properties(self) -> list[str]:
        """
        获取本体中所有数据属性的名称列表。

        Returns:
            数据属性名称列表。
        """
        return [prop.name for prop in self.ontology.data_properties()]

    def get_class_hierarchy(self) -> dict[str, list[str]]:
        """
        获取类的层次继承关系。

        Returns:
            字典，键为父类名，值为直接子类名列表。
        """
        hierarchy: dict[str, list[str]] = {}
        for cls in self.ontology.classes():
            for parent in cls.is_a:
                # 只处理命名类（排除匿名限制类）
                if hasattr(parent, "name"):
                    parent_name = parent.name
                    if parent_name not in hierarchy:
                        hierarchy[parent_name] = []
                    hierarchy[parent_name].append(cls.name)
        return hierarchy

    def get_ontology_summary(self) -> dict[str, Any]:
        """
        获取本体摘要信息，用于 RAG 上下文注入。

        Returns:
            包含类、属性、层次结构的摘要字典。
        """
        return {
            "iri": str(self.ontology.base_iri),
            "classes": self.get_all_classes(),
            "object_properties": self.get_all_object_properties(),
            "data_properties": self.get_all_data_properties(),
            "hierarchy": self.get_class_hierarchy(),
        }
