"""OWL 本体 loader 和 reasoner 额外覆盖测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.ontology.loader import OntologyLoader
from core.ontology.reasoner import OntologyReasoner, P2P_RULES, RuleViolation


class TestOntologyLoaderExtra:
    """OntologyLoader 额外测试。"""

    def test_loader_is_loaded_property(self) -> None:
        """未加载时 is_loaded 应为 False。"""
        loader = OntologyLoader()
        assert loader.is_loaded is False

    def test_owlready2_not_available(self) -> None:
        """owlready2 不可用时 load 应抛出 RuntimeError。"""
        with patch("core.ontology.loader.OWLREADY2_AVAILABLE", False):
            loader = OntologyLoader()
            with pytest.raises(RuntimeError, match="owlready2 未安装"):
                loader.load()

    def test_get_class(self) -> None:
        """get_class 应查询本体类。"""
        loader = OntologyLoader()
        mock_onto = MagicMock()
        mock_onto.__getitem__ = MagicMock(return_value="PO_CLASS")
        loader._ontology = mock_onto
        loader._loaded = True

        result = loader.get_class("PurchaseOrder")
        assert result == "PO_CLASS"

    def test_get_all_classes(self) -> None:
        """get_all_classes 应返回类名列表。"""
        loader = OntologyLoader()
        mock_cls1 = MagicMock()
        mock_cls1.name = "Supplier"
        mock_cls2 = MagicMock()
        mock_cls2.name = "PurchaseOrder"
        mock_onto = MagicMock()
        mock_onto.classes.return_value = [mock_cls1, mock_cls2]
        loader._ontology = mock_onto
        loader._loaded = True

        classes = loader.get_all_classes()
        assert classes == ["Supplier", "PurchaseOrder"]

    def test_get_all_object_properties(self) -> None:
        """get_all_object_properties 应返回属性列表。"""
        loader = OntologyLoader()
        mock_prop = MagicMock()
        mock_prop.name = "issuedBy"
        mock_onto = MagicMock()
        mock_onto.object_properties.return_value = [mock_prop]
        loader._ontology = mock_onto
        loader._loaded = True

        props = loader.get_all_object_properties()
        assert props == ["issuedBy"]

    def test_get_all_data_properties(self) -> None:
        """get_all_data_properties 应返回属性列表。"""
        loader = OntologyLoader()
        mock_prop = MagicMock()
        mock_prop.name = "amount"
        mock_onto = MagicMock()
        mock_onto.data_properties.return_value = [mock_prop]
        loader._ontology = mock_onto
        loader._loaded = True

        props = loader.get_all_data_properties()
        assert props == ["amount"]

    def test_get_class_hierarchy(self) -> None:
        """get_class_hierarchy 应返回层次结构。"""
        loader = OntologyLoader()

        parent_cls = MagicMock()
        parent_cls.name = "Document"

        child_cls = MagicMock()
        child_cls.name = "Invoice"
        child_cls.is_a = [parent_cls]

        mock_onto = MagicMock()
        mock_onto.classes.return_value = [child_cls]
        loader._ontology = mock_onto
        loader._loaded = True

        hierarchy = loader.get_class_hierarchy()
        assert "Document" in hierarchy
        assert "Invoice" in hierarchy["Document"]

    def test_get_class_hierarchy_anonymous_parent(self) -> None:
        """匿名限制类（无 name 属性）应被跳过。"""
        loader = OntologyLoader()

        anon_parent = MagicMock(spec=[])  # 没有 name 属性

        child_cls = MagicMock()
        child_cls.name = "Invoice"
        child_cls.is_a = [anon_parent]

        mock_onto = MagicMock()
        mock_onto.classes.return_value = [child_cls]
        loader._ontology = mock_onto
        loader._loaded = True

        hierarchy = loader.get_class_hierarchy()
        assert hierarchy == {}

    def test_get_ontology_summary(self) -> None:
        """get_ontology_summary 应返回摘要字典。"""
        loader = OntologyLoader()

        mock_onto = MagicMock()
        mock_onto.base_iri = "http://test.io/onto#"
        mock_cls = MagicMock()
        mock_cls.name = "PO"
        mock_cls.is_a = []
        mock_onto.classes.return_value = [mock_cls]
        mock_onto.object_properties.return_value = []
        mock_onto.data_properties.return_value = []
        loader._ontology = mock_onto
        loader._loaded = True

        summary = loader.get_ontology_summary()
        assert "iri" in summary
        assert summary["classes"] == ["PO"]

    def test_world_property_not_loaded(self) -> None:
        """未加载时访问 world 应抛出 RuntimeError。"""
        loader = OntologyLoader()
        with pytest.raises(RuntimeError, match="尚未加载"):
            _ = loader.world


class TestOntologyReasonerExtra:
    """OntologyReasoner 额外测试。"""

    def test_reasoner_init_pellet_unavailable_hermit_available(self) -> None:
        """Pellet 不可用但 HermiT 可用时应回退到 HermiT。"""
        loader = MagicMock()
        with patch("core.ontology.reasoner.OWLREADY2_AVAILABLE", True):
            def import_mock(name, *args, **kwargs):
                if "pellet" in name:
                    raise ImportError("no pellet")
                return MagicMock()

            with patch("builtins.__import__", side_effect=import_mock):
                # 创建 reasoner 不应抛异常
                reasoner = OntologyReasoner.__new__(OntologyReasoner)
                reasoner._loader = loader
                reasoner._reasoner_available = False
                reasoner._sync_reasoner = None

    def test_get_rules_context_for_rag(self) -> None:
        """get_rules_context_for_rag 应返回格式化的规则文本。"""
        loader = MagicMock()
        with patch("core.ontology.reasoner.OWLREADY2_AVAILABLE", False):
            reasoner = OntologyReasoner(loader)

        context = reasoner.get_rules_context_for_rag()
        assert "P2P 核心业务合规规则" in context
        assert "三路匹配金额偏差规则" in context
        assert "RULE_P2P_THREE_WAY_MATCH_AMOUNT" in context

    def test_get_ontology_context_for_agent(self) -> None:
        """get_ontology_context_for_agent 应返回混合上下文。"""
        loader = MagicMock()
        with patch("core.ontology.reasoner.OWLREADY2_AVAILABLE", False):
            reasoner = OntologyReasoner(loader)

        ctx = reasoner.get_ontology_context_for_agent()
        assert "structured" in ctx
        assert "narrative" in ctx
        assert "P2P" in ctx["structured"]["domain"]
        assert len(ctx["structured"]["core_entities"]) == 6
        assert len(ctx["structured"]["compliance_rules"]) == len(P2P_RULES)

    def test_run_reasoning_unavailable(self) -> None:
        """推理器不可用时 run_reasoning 应返回 False。"""
        loader = MagicMock()
        with patch("core.ontology.reasoner.OWLREADY2_AVAILABLE", False):
            reasoner = OntologyReasoner(loader)
        result = reasoner.run_reasoning()
        assert result is False

    def test_rule_violation_dataclass(self) -> None:
        """RuleViolation 数据类应正确初始化。"""
        v = RuleViolation(
            rule_id="R1",
            rule_name="测试规则",
            subject_id="PO-001",
            subject_type="PurchaseOrder",
            details={"variance": 0.1},
        )
        assert v.rule_id == "R1"
        assert v.detected_at is not None
