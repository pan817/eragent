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
from core.ontology.reasoner import OntologyReasoner, P2P_RULES


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
