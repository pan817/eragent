"""LLM 客户端构造工厂。

把 ChatOpenAI 的构造从 P2PAgent 主类剥离，便于单测和未来切换 provider。
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from config.settings import LLMSettings


def build_chat_model(llm_cfg: LLMSettings) -> ChatOpenAI:
    """根据配置构造 ChatOpenAI 兼容客户端。

    当 ``llm_cfg.use_system_proxy`` 为 False 时显式构造
    ``trust_env=False`` 的 httpx AsyncClient，强制直连，避免系统代理
    （HTTP_PROXY/HTTPS_PROXY）干扰。
    """
    import httpx

    kwargs: dict[str, Any] = dict(
        model=llm_cfg.model,
        api_key=llm_cfg.api_key,
        base_url=llm_cfg.api_base,
        temperature=llm_cfg.temperature,
        max_tokens=llm_cfg.max_tokens,
        timeout=llm_cfg.timeout,
    )
    if not llm_cfg.use_system_proxy:
        # 当前服务全程走 async 路径，仅创建 AsyncClient，避免空闲的同步连接池。
        kwargs["http_async_client"] = httpx.AsyncClient(
            trust_env=False, timeout=llm_cfg.timeout
        )
    return ChatOpenAI(**kwargs)
