"""Embedding Provider 抽象层单元测试。"""

from __future__ import annotations

import pytest

from core.knowledge.embeddings import (
    DefaultChromaEmbeddingProvider,
    EmbeddingProvider,
    FakeEmbeddingProvider,
    build_embedding_provider,
    to_chroma_embedding_function,
)


class TestFakeEmbeddingProvider:
    """FakeEmbeddingProvider 行为测试。"""

    def test_dimension_default(self) -> None:
        provider = FakeEmbeddingProvider()
        vec = provider.embed_query("hello")
        assert len(vec) == 16

    def test_dimension_custom(self) -> None:
        provider = FakeEmbeddingProvider(dimension=8)
        vec = provider.embed_query("hello")
        assert len(vec) == 8

    def test_deterministic(self) -> None:
        """相同输入应返回相同向量。"""
        provider = FakeEmbeddingProvider()
        assert provider.embed_query("foo") == provider.embed_query("foo")

    def test_different_inputs_differ(self) -> None:
        provider = FakeEmbeddingProvider()
        assert provider.embed_query("foo") != provider.embed_query("bar")

    def test_embed_documents(self) -> None:
        provider = FakeEmbeddingProvider()
        vecs = provider.embed_documents(["a", "b", "c"])
        assert len(vecs) == 3
        assert all(len(v) == 16 for v in vecs)

    def test_protocol_compliance(self) -> None:
        """FakeEmbeddingProvider 应满足 EmbeddingProvider 协议。"""
        provider = FakeEmbeddingProvider()
        assert isinstance(provider, EmbeddingProvider)


class TestBuildEmbeddingProvider:
    """build_embedding_provider 工厂测试。"""

    def test_default(self) -> None:
        provider = build_embedding_provider("default")
        assert isinstance(provider, DefaultChromaEmbeddingProvider)

    def test_fake(self) -> None:
        provider = build_embedding_provider("fake", dimension=4)
        assert isinstance(provider, FakeEmbeddingProvider)
        assert len(provider.embed_query("x")) == 4

    def test_unknown(self) -> None:
        with pytest.raises(ValueError, match="未知的 embedding provider"):
            build_embedding_provider("nonexistent")

    def test_openai_requires_model(self) -> None:
        with pytest.raises(ValueError, match="model"):
            build_embedding_provider("openai", api_key="x")

    def test_openai_requires_api_key(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            build_embedding_provider("openai", model="text-embedding-3-small")

    def test_openai_constructed(self) -> None:
        """openai provider 不应在构造期请求网络。"""
        provider = build_embedding_provider(
            "openai",
            model="text-embedding-3-small",
            api_key="sk-test",
            api_base="https://example.com",
        )
        assert provider.name == "openai_compatible"


class TestChromaAdapter:
    """to_chroma_embedding_function 适配器测试。"""

    def test_fake_adapter_callable(self) -> None:
        provider = FakeEmbeddingProvider(dimension=8)
        fn = to_chroma_embedding_function(provider)
        result = fn(["a", "b"])
        assert len(result) == 2
        assert len(result[0]) == 8

    def test_fake_adapter_name(self) -> None:
        provider = FakeEmbeddingProvider()
        fn = to_chroma_embedding_function(provider)
        assert fn.name() == "fake"
