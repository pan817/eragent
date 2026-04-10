"""config.crypto 与 Settings 加密集成单元测试。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import InvalidToken

from config.crypto import decrypt, encrypt, generate_master_key
from config.settings import Settings


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽 from_yaml 内的 load_dotenv 调用，防止真实 .env 文件干扰测试。

    from_yaml 内以 `from dotenv import load_dotenv` 方式局部导入，
    所以直接 patch 顶层 dotenv 模块即可拦截所有调用。
    """
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: None)


# ---------------------------------------------------------------------------
# config.crypto 核心函数
# ---------------------------------------------------------------------------


class TestGenerateMasterKey:
    def test_returns_string(self) -> None:
        key = generate_master_key()
        assert isinstance(key, str)

    def test_length_is_44(self) -> None:
        # Fernet key = 32 bytes base64url = 44 字符（含填充）
        key = generate_master_key()
        assert len(key) == 44

    def test_each_call_produces_unique_key(self) -> None:
        assert generate_master_key() != generate_master_key()

    def test_key_is_valid_for_encrypt(self) -> None:
        key = generate_master_key()
        # 能正常加密即表示格式合法
        ciphertext = encrypt("hello", key)
        assert ciphertext


class TestEncryptDecrypt:
    def test_roundtrip(self) -> None:
        key = generate_master_key()
        plaintext = "secret-password-123"
        assert decrypt(encrypt(plaintext, key), key) == plaintext

    def test_ciphertext_differs_from_plaintext(self) -> None:
        key = generate_master_key()
        plaintext = "my_password"
        assert encrypt(plaintext, key) != plaintext

    def test_same_plaintext_produces_different_ciphertexts(self) -> None:
        # Fernet 每次加密都加入随机 IV，密文应不同
        key = generate_master_key()
        c1 = encrypt("same", key)
        c2 = encrypt("same", key)
        assert c1 != c2

    def test_encrypt_with_invalid_key_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="格式无效"):
            encrypt("hello", "not-a-valid-fernet-key")

    def test_decrypt_with_wrong_key_raises_invalid_token(self) -> None:
        key1 = generate_master_key()
        key2 = generate_master_key()
        ciphertext = encrypt("secret", key1)
        with pytest.raises(InvalidToken):
            decrypt(ciphertext, key2)

    def test_decrypt_tampered_ciphertext_raises_invalid_token(self) -> None:
        key = generate_master_key()
        ciphertext = encrypt("secret", key)
        tampered = ciphertext[:-4] + "XXXX"
        with pytest.raises(InvalidToken):
            decrypt(tampered, key)

    def test_decrypt_with_invalid_key_raises_valueerror(self) -> None:
        key = generate_master_key()
        ciphertext = encrypt("secret", key)
        with pytest.raises(ValueError, match="格式无效"):
            decrypt(ciphertext, "bad-key")

    def test_unicode_plaintext_roundtrip(self) -> None:
        key = generate_master_key()
        plaintext = "密码-αβγ-🔑"
        assert decrypt(encrypt(plaintext, key), key) == plaintext


# ---------------------------------------------------------------------------
# Settings.from_yaml 加密集成
# ---------------------------------------------------------------------------

_MINIMAL_YAML: dict = {}  # 空 yaml，全用默认值


def _write_yaml(tmp_path: Path, content: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(content), encoding="utf-8")
    return p


class TestSettingsPlainPassword:
    """向后兼容：明文模式与现有行为一致。"""

    def test_plain_postgres_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        monkeypatch.setenv("POSTGRES_PASSWORD", "devpass")
        monkeypatch.delenv("POSTGRES_PASSWORD_ENCRYPTED", raising=False)
        monkeypatch.delenv("LLM_API_KEY_ENCRYPTED", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD_ENCRYPTED", raising=False)
        s = Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))
        assert s.postgresql.password == "devpass"

    def test_plain_llm_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        monkeypatch.setenv("LLM_API_KEY", "sk-test-key")
        monkeypatch.delenv("LLM_API_KEY_ENCRYPTED", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD_ENCRYPTED", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD_ENCRYPTED", raising=False)
        s = Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))
        assert s.llm.api_key == "sk-test-key"

    def test_plain_neo4j_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        monkeypatch.setenv("NEO4J_PASSWORD", "neo4j-secret")
        monkeypatch.delenv("NEO4J_PASSWORD_ENCRYPTED", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD_ENCRYPTED", raising=False)
        monkeypatch.delenv("LLM_API_KEY_ENCRYPTED", raising=False)
        s = Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))
        assert s.neo4j.password == "neo4j-secret"

    def test_no_credentials_uses_empty_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        for key in ("POSTGRES_PASSWORD", "POSTGRES_PASSWORD_ENCRYPTED",
                    "LLM_API_KEY", "LLM_API_KEY_ENCRYPTED",
                    "NEO4J_PASSWORD", "NEO4J_PASSWORD_ENCRYPTED",
                    "ENCRYPTION_MASTER_KEY"):
            monkeypatch.delenv(key, raising=False)
        s = Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))
        assert s.postgresql.password == ""
        assert s.llm.api_key == ""
        assert s.neo4j.password == ""


class TestSettingsEncryptedPassword:
    """加密模式：密文 + 主密钥 → 解密后注入字段。"""

    def test_encrypted_postgres_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        master_key = generate_master_key()
        ciphertext = encrypt("prod-db-pass", master_key)
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY", master_key)
        monkeypatch.setenv("POSTGRES_PASSWORD_ENCRYPTED", ciphertext)
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY_ENCRYPTED", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD_ENCRYPTED", raising=False)
        s = Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))
        assert s.postgresql.password == "prod-db-pass"

    def test_encrypted_llm_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        master_key = generate_master_key()
        ciphertext = encrypt("sk-prod-key", master_key)
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY", master_key)
        monkeypatch.setenv("LLM_API_KEY_ENCRYPTED", ciphertext)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD_ENCRYPTED", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD_ENCRYPTED", raising=False)
        s = Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))
        assert s.llm.api_key == "sk-prod-key"

    def test_encrypted_neo4j_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        master_key = generate_master_key()
        ciphertext = encrypt("neo4j-prod", master_key)
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY", master_key)
        monkeypatch.setenv("NEO4J_PASSWORD_ENCRYPTED", ciphertext)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY_ENCRYPTED", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD_ENCRYPTED", raising=False)
        s = Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))
        assert s.neo4j.password == "neo4j-prod"

    def test_all_three_encrypted_same_master_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        master_key = generate_master_key()
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY", master_key)
        for env_plain, env_enc, val in (
            ("POSTGRES_PASSWORD", "POSTGRES_PASSWORD_ENCRYPTED", "db-pass"),
            ("LLM_API_KEY",       "LLM_API_KEY_ENCRYPTED",       "llm-key"),
            ("NEO4J_PASSWORD",    "NEO4J_PASSWORD_ENCRYPTED",    "neo-pass"),
        ):
            monkeypatch.delenv(env_plain, raising=False)
            monkeypatch.setenv(env_enc, encrypt(val, master_key))
        s = Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))
        assert s.postgresql.password == "db-pass"
        assert s.llm.api_key == "llm-key"
        assert s.neo4j.password == "neo-pass"

    def test_dsn_uses_decrypted_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        master_key = generate_master_key()
        ciphertext = encrypt("secret123", master_key)
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY", master_key)
        monkeypatch.setenv("POSTGRES_PASSWORD_ENCRYPTED", ciphertext)
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY_ENCRYPTED", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD_ENCRYPTED", raising=False)
        s = Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))
        assert "secret123" in s.postgresql.dsn
        assert "secret123" in s.postgresql.conninfo


class TestSettingsEncryptionErrors:
    """错误场景：配置冲突或密钥缺失时必须明确报错，不允许静默降级。"""

    def test_both_plain_and_encrypted_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        master_key = generate_master_key()
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY", master_key)
        monkeypatch.setenv("POSTGRES_PASSWORD", "plain")
        monkeypatch.setenv("POSTGRES_PASSWORD_ENCRYPTED", encrypt("plain", master_key))
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY_ENCRYPTED", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD_ENCRYPTED", raising=False)
        with pytest.raises(ValueError, match="不能同时设置"):
            Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))

    def test_llm_both_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        master_key = generate_master_key()
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY", master_key)
        monkeypatch.setenv("LLM_API_KEY", "plain-key")
        monkeypatch.setenv("LLM_API_KEY_ENCRYPTED", encrypt("plain-key", master_key))
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD_ENCRYPTED", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD_ENCRYPTED", raising=False)
        with pytest.raises(ValueError, match="不能同时设置"):
            Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))

    def test_encrypted_without_master_key_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        monkeypatch.delenv("ENCRYPTION_MASTER_KEY", raising=False)
        monkeypatch.setenv("POSTGRES_PASSWORD_ENCRYPTED", "gAAAAAnot-real-but-non-empty")
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY_ENCRYPTED", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD_ENCRYPTED", raising=False)
        with pytest.raises(ValueError, match="ENCRYPTION_MASTER_KEY"):
            Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))

    def test_wrong_master_key_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dotenv: None
    ) -> None:
        key1 = generate_master_key()
        key2 = generate_master_key()
        ciphertext = encrypt("secret", key1)
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY", key2)  # 错误的 key
        monkeypatch.setenv("POSTGRES_PASSWORD_ENCRYPTED", ciphertext)
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY_ENCRYPTED", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD_ENCRYPTED", raising=False)
        with pytest.raises(ValueError, match="解密失败"):
            Settings.from_yaml(_write_yaml(tmp_path, _MINIMAL_YAML))


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------


class TestCLI:
    def test_generate_key_outputs_key(self, capsys: pytest.CaptureFixture) -> None:
        from config.crypto import main
        main(["generate-key"])
        out = capsys.readouterr().out.strip()
        assert len(out) == 44  # Fernet key 长度

    def test_encrypt_cmd(self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
        from config.crypto import main
        key = generate_master_key()
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY", key)
        monkeypatch.setenv("MY_SECRET", "hello-world")
        main(["encrypt", "MY_SECRET"])
        out = capsys.readouterr().out.strip()
        # 输出的密文应可被还原
        assert decrypt(out, key) == "hello-world"

    def test_decrypt_cmd(self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
        from config.crypto import main
        key = generate_master_key()
        ciphertext = encrypt("restored-value", key)
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY", key)
        main(["decrypt", ciphertext])
        out = capsys.readouterr().out.strip()
        assert out == "restored-value"

    def test_no_args_exits_nonzero(self) -> None:
        from config.crypto import main
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code != 0

    def test_unknown_command_exits_nonzero(self) -> None:
        from config.crypto import main
        with pytest.raises(SystemExit) as exc_info:
            main(["unknown-cmd"])
        assert exc_info.value.code != 0

    def test_encrypt_missing_master_key_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from config.crypto import main
        monkeypatch.delenv("ENCRYPTION_MASTER_KEY", raising=False)
        monkeypatch.setenv("MY_SECRET", "val")
        with pytest.raises(SystemExit) as exc_info:
            main(["encrypt", "MY_SECRET"])
        assert exc_info.value.code != 0

    def test_encrypt_missing_env_var_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from config.crypto import main
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY", generate_master_key())
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            main(["encrypt", "NONEXISTENT_VAR"])
        assert exc_info.value.code != 0
