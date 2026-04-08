"""
Embedding Provider 抽象层。

将 Embedding 模型与 VectorStore 解耦，支持运行时按配置切换：
- ``default``: 使用 Chroma 内置默认 Embedding（all-MiniLM-L6-v2，本地）。
- ``openai``: 走 OpenAI 兼容接口（如智谱、DeepSeek、Qwen DashScope 兼容模式）。
- ``dashscope``: 阿里 DashScope text-embedding-v3 原生接口。
- ``fake``: 测试专用，返回确定性向量。

设计要点：
- 所有 Provider 实现 ``EmbeddingProvider`` 协议（``embed_documents`` / ``embed_query``）。
- ``to_chroma_embedding_function`` 将 Provider 适配为 Chroma 期望的 EmbeddingFunction，
  避免侵入 Chroma 的 Collection 创建流程。
- ``build_embedding_provider`` 是配置驱动的工厂入口，由 VectorStore 调用。
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embedding 提供方协议。"""

    name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量将文档文本转为向量。"""
        ...

    def embed_query(self, text: str) -> list[float]:
        """将查询文本转为单个向量。"""
        ...


class FakeEmbeddingProvider:
    """
    确定性 Fake Embedding，用于单元测试。

    通过 SHA-256 哈希将文本映射为固定维度的向量，相同输入始终返回相同向量，
    便于测试断言。
    """

    name = "fake"

    def __init__(self, dimension: int = 16) -> None:
        self._dimension = dimension

    def _hash_to_vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 取前 dimension 字节并归一化到 [-1, 1]
        vec = [(b - 128) / 128.0 for b in digest[: self._dimension]]
        # 不足时循环填充
        while len(vec) < self._dimension:
            vec.append(0.0)
        return vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._hash_to_vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._hash_to_vector(text)


class DefaultChromaEmbeddingProvider:
    """
    使用 Chroma 内置默认 Embedding（all-MiniLM-L6-v2）。

    该 Provider 不主动计算向量，而是由 Chroma Collection 在 add/query 时
    透明完成。``embed_documents`` / ``embed_query`` 在直接调用时才会加载模型。
    """

    name = "default"

    def __init__(self) -> None:
        self._fn: Any = None

    def _ensure_fn(self) -> Any:
        if self._fn is None:
            from chromadb.utils import embedding_functions  # type: ignore[import-untyped]

            self._fn = embedding_functions.DefaultEmbeddingFunction()
        return self._fn

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return list(self._ensure_fn()(texts))

    def embed_query(self, text: str) -> list[float]:
        return list(self._ensure_fn()([text])[0])


def to_chroma_embedding_function(provider: EmbeddingProvider) -> Any:
    """
    将 EmbeddingProvider 适配为 Chroma 的 EmbeddingFunction。

    Chroma 的 EmbeddingFunction 协议要求实现 ``__call__(input)`` 与 ``name()``。
    本函数返回一个轻量适配器实例，避免在 Provider 类上直接绑定 Chroma 协议。

    对于 ``default`` Provider，直接返回 Chroma 自带的 DefaultEmbeddingFunction，
    避免多包一层带来的性能损耗。
    """
    if isinstance(provider, DefaultChromaEmbeddingProvider):
        return provider._ensure_fn()

    class _Adapter:
        def __init__(self, p: EmbeddingProvider) -> None:
            self._p = p

        def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002
            return self._p.embed_documents(list(input))

        def name(self) -> str:
            return self._p.name

    return _Adapter(provider)


def build_embedding_provider(
    provider_name: str = "default",
    model: str | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    **kwargs: Any,
) -> EmbeddingProvider:
    """
    根据配置构造 EmbeddingProvider。

    Args:
        provider_name: ``default`` / ``fake`` / ``openai`` / ``dashscope``。
        model: 模型名（如 ``text-embedding-3-small``），仅远端 Provider 使用。
        api_key: 远端 Provider 的 API Key。
        api_base: 远端 Provider 的 Base URL。
        **kwargs: 透传给具体 Provider 的额外参数（如 ``dimension``）。

    Returns:
        实现 ``EmbeddingProvider`` 协议的实例。

    Raises:
        ValueError: provider_name 未识别，或远端 Provider 缺少必需参数。
    """
    name = provider_name.lower().strip()

    if name == "default":
        return DefaultChromaEmbeddingProvider()

    if name == "fake":
        return FakeEmbeddingProvider(dimension=int(kwargs.get("dimension", 16)))

    if name in {"openai", "dashscope", "zhipu"}:
        # 远端 Provider 在真正使用时才需要 SDK，此处仅校验参数并延迟导入
        if not model:
            raise ValueError(f"{name} embedding provider 需要指定 model")
        if not api_key:
            raise ValueError(f"{name} embedding provider 需要 api_key")
        return _OpenAICompatibleEmbeddingProvider(
            model=model,
            api_key=api_key,
            api_base=api_base,
        )

    raise ValueError(f"未知的 embedding provider: {provider_name}")


class _OpenAICompatibleEmbeddingProvider:
    """
    通过 OpenAI 兼容接口调用远端 Embedding 服务。

    适用于智谱 / DeepSeek / Qwen DashScope 兼容模式 / OpenAI 等。
    实际请求时才导入 ``openai`` SDK，避免无远端依赖时的导入失败。
    """

    name = "openai_compatible"

    def __init__(self, model: str, api_key: str, api_base: str | None) -> None:
        self._model = model
        self._api_key = api_key
        self._api_base = api_base
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI  # type: ignore[import-not-found]

            self._client = OpenAI(api_key=self._api_key, base_url=self._api_base)
        return self._client

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._ensure_client().embeddings.create(
            model=self._model,
            input=texts,
        )
        return [item.embedding for item in resp.data]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]
