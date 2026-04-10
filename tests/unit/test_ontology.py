"""
OntologyLoader 和 OntologyReasoner 单元测试。

OntologyLoader.load() 依赖 owlready2，若不可用则跳过相关测试。
OntologyReasoner 的规则查询方法使用 Python 内置规则字典，不依赖本体文件。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.ontology.loader import OntologyLoader, OWLREADY2_AVAILABLE
from core.ontology.reasoner import OntologyReasoner, P2P_RULES, RuleViolation


# ============================================================
# OntologyLoader 测试
# ============================================================


class TestOntologyLoader:
    """测试 OntologyLoader。"""

    def test_loader_default_path(self) -> None:
        """默认 OWL 文件路径应指向 modules/p2p/ontology/p2p.owl。"""
        loader = OntologyLoader()
        expected_suffix = Path("modules") / "p2p" / "ontology" / "p2p.owl"
        assert str(loader._owl_path).endswith(str(expected_suffix))

    def test_loader_custom_path(self, tmp_path: Path) -> None:
        """支持自定义 OWL 文件路径。"""
        custom_path = tmp_path / "custom.owl"
        loader = OntologyLoader(owl_file_path=custom_path)
        assert loader._owl_path == custom_path

    def test_loader_file_not_found(self, tmp_path: Path) -> None:
        """OWL 文件不存在时 load() 应抛出 FileNotFoundError。"""
        loader = OntologyLoader(owl_file_path=tmp_path / "nonexistent.owl")
        if not OWLREADY2_AVAILABLE:
            with pytest.raises(RuntimeError, match="owlready2"):
                loader.load()
        else:
            with pytest.raises(FileNotFoundError):
                loader.load()

    def test_loader_not_loaded_error(self) -> None:
        """未调用 load() 时访问 ontology 属性应抛出 RuntimeError。"""
        loader = OntologyLoader()
        assert loader.is_loaded is False
        with pytest.raises(RuntimeError, match="尚未加载"):
            _ = loader.ontology
        with pytest.raises(RuntimeError, match="尚未加载"):
            _ = loader.world

    @pytest.mark.skipif(
        not OWLREADY2_AVAILABLE,
        reason="owlready2 未安装，跳过需要真实本体加载的测试",
    )
    def test_loader_load_with_real_owl(self, tmp_path: Path) -> None:
        """如果 owlready2 可用，使用最小 OWL 文件测试加载。"""
        owl_content = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xml:base="http://test.example.org/ontology">
  <owl:Ontology rdf:about="http://test.example.org/ontology"/>
  <owl:Class rdf:about="http://test.example.org/ontology#TestClass"/>
</rdf:RDF>"""
        owl_file = tmp_path / "test.owl"
        owl_file.write_text(owl_content, encoding="utf-8")

        loader = OntologyLoader(owl_file_path=owl_file)
        loader.load()
        assert loader.is_loaded is True
        assert loader.ontology is not None


# ============================================================
# OntologyReasoner 测试
# ============================================================


class TestOntologyReasoner:
    """测试 OntologyReasoner 的规则查询功能。"""

    @pytest.fixture()
    def reasoner(self) -> OntologyReasoner:
        """创建 OntologyReasoner 实例（mock OntologyLoader）。"""
        mock_loader = MagicMock(spec=OntologyLoader)
        mock_loader.is_loaded = False
        # 在非 owlready2 环境下，_init_reasoner 不会尝试导入推理器
        with patch("core.ontology.reasoner.OWLREADY2_AVAILABLE", False):
            return OntologyReasoner(mock_loader)

    def test_reasoner_get_rule_by_id(self, reasoner: OntologyReasoner) -> None:
        """按规则 ID 获取规则元数据。"""
        rule = reasoner.get_rule_by_id("RULE_P2P_THREE_WAY_MATCH_AMOUNT")
        assert rule is not None
        assert rule["name"] == "三路匹配金额偏差规则"
        assert rule["category"] == "three_way_match"

    def test_reasoner_get_rule_by_id_not_found(self, reasoner: OntologyReasoner) -> None:
        """不存在的规则 ID 应返回 None。"""
        assert reasoner.get_rule_by_id("NONEXISTENT_RULE") is None

    def test_reasoner_get_rules_by_category(self, reasoner: OntologyReasoner) -> None:
        """按分类获取规则列表。"""
        twm_rules = reasoner.get_rules_by_category("three_way_match")
        assert len(twm_rules) == 2
        assert "RULE_P2P_THREE_WAY_MATCH_AMOUNT" in twm_rules
        assert "RULE_P2P_THREE_WAY_MATCH_QUANTITY" in twm_rules

        pc_rules = reasoner.get_rules_by_category("payment_compliance")
        assert len(pc_rules) == 3

    def test_reasoner_get_all_rules(self, reasoner: OntologyReasoner) -> None:
        """获取全部规则应返回所有 P2P_RULES。"""
        all_rules = reasoner.get_all_rules()
        assert len(all_rules) == len(P2P_RULES)

    def test_reasoner_context_for_agent(self, reasoner: OntologyReasoner) -> None:
        """Agent 上下文应包含 structured 和 narrative 两个键。"""
        context = reasoner.get_ontology_context_for_agent()
        assert "structured" in context
        assert "narrative" in context
        structured = context["structured"]
        assert structured["domain"] == "P2P (Procure-to-Pay)"
        assert "core_entities" in structured
        assert "compliance_rules" in structured
        assert len(structured["compliance_rules"]) == len(P2P_RULES)

    def test_reasoner_rules_context_for_rag(self, reasoner: OntologyReasoner) -> None:
        """RAG 上下文应为格式化的规则描述文本。"""
        text = reasoner.get_rules_context_for_rag()
        assert "P2P 核心业务合规规则" in text
        assert "三路匹配金额偏差规则" in text
        assert "RULE_P2P_THREE_WAY_MATCH_AMOUNT" in text

    def test_reasoner_run_reasoning_without_reasoner(self, reasoner: OntologyReasoner) -> None:
        """推理器不可用时 run_reasoning 应返回 False。"""
        assert reasoner.run_reasoning() is False


# ============================================================
# OntologyLoader 额外覆盖测试
# ============================================================


class TestOntologyLoaderExtra:
    """OntologyLoader 额外边界覆盖。"""

    def test_loader_is_loaded_property_false(self) -> None:
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


# ============================================================
# OntologyReasoner 额外覆盖测试
# ============================================================


class TestOntologyReasonerExtra:
    """OntologyReasoner 额外边界覆盖。"""

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
