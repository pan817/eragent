"""
config.settings 模块单元测试。

测试 Settings 的默认值、YAML 加载、子配置和校验逻辑。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from config.settings import (
    LoggingSettings,
    P2PSettings,
    Settings,
)


class TestDefaultSettings:
    """测试 Settings 默认配置值。"""

    def test_default_settings(self) -> None:
        """默认 Settings 应包含预设的 app_name、版本和子配置。"""
        s = Settings()
        assert s.app_name == "ERP Analysis Agent"
        assert s.app_version == "0.1.0"
        assert s.debug is False
        assert s.language == "zh"
        assert s.llm.provider == "zhipu"
        assert s.llm.model == "glm-4"
        assert s.neo4j.uri == "bolt://localhost:7687"
        assert s.postgresql.port == 5432
        assert s.memory.short_term_max_messages == 20

    def test_p2p_tolerances(self) -> None:
        """P2P 容差配置的默认值应正确。"""
        s = Settings()
        p2p = s.p2p
        assert p2p.three_way_match.default_tolerance_pct == 5.0
        assert p2p.three_way_match.max_tolerance_pct == 10.0
        assert p2p.payment_compliance.early_payment_threshold_days == 5
        assert p2p.supplier_performance.otif_rate == 95.0
        assert p2p.anomaly_severity.high_amount_threshold == 500000.0
        assert p2p.anomaly_severity.variance_high_multiplier == 2.0


class TestFromYaml:
    """测试 Settings.from_yaml 加载能力。"""

    def test_from_yaml(self, tmp_path: Path) -> None:
        """从合法 YAML 文件加载配置应正确覆盖默认值。"""
        yaml_content = {
            "app": {
                "name": "Test App",
                "version": "1.0.0",
                "debug": True,
                "language": "en",
            },
            "llm": {
                "provider": "openai",
                "model": "gpt-4",
            },
            "p2p": {
                "three_way_match": {
                    "default_tolerance_pct": 3.0,
                },
                "payment_compliance": {},
                "supplier_performance": {"benchmarks": {}},
                "anomaly_severity": {},
            },
            "memory": {
                "short_term": {"max_messages": 30},
                "long_term": {"max_retrieved": 10},
            },
        }
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(yaml.dump(yaml_content), encoding="utf-8")

        s = Settings.from_yaml(yaml_path)
        assert s.app_name == "Test App"
        assert s.app_version == "1.0.0"
        assert s.debug is True
        assert s.llm.provider == "openai"
        assert s.p2p.three_way_match.default_tolerance_pct == 3.0
        assert s.memory.short_term_max_messages == 30

    def test_from_yaml_nonexistent_file(self, tmp_path: Path) -> None:
        """YAML 文件不存在时应使用全部默认值。"""
        s = Settings.from_yaml(tmp_path / "nonexistent.yaml")
        assert s.app_name == "ERP Analysis Agent"

    def test_from_yaml_empty_file(self, tmp_path: Path) -> None:
        """YAML 文件内容为空时应使用全部默认值。"""
        empty_yaml = tmp_path / "empty.yaml"
        empty_yaml.write_text("", encoding="utf-8")
        s = Settings.from_yaml(empty_yaml)
        assert s.app_name == "ERP Analysis Agent"


class TestLoggingLevelValidation:
    """测试日志级别校验。"""

    def test_logging_level_validation_valid(self) -> None:
        """合法的日志级别应被接受并转为大写。"""
        for level in ("debug", "INFO", "Warning", "ERROR", "critical"):
            ls = LoggingSettings(level=level)
            assert ls.level == level.upper()

    def test_logging_level_validation_invalid(self) -> None:
        """非法日志级别应抛出 ValueError。"""
        with pytest.raises(Exception):
            LoggingSettings(level="TRACE")


class TestPostgreSQLDSN:
    """测试 PostgreSQL DSN 生成。"""

    def test_dsn_property(self) -> None:
        """DSN 属性应正确拼接连接字符串。"""
        s = Settings()
        dsn = s.postgresql.dsn
        assert "postgresql+psycopg2://" in dsn
        assert "localhost" in dsn
        assert "5432" in dsn
