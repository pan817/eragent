"""LLM 客户端构造工厂。

把 ChatOpenAI 的构造从 P2PAgent 主类剥离，便于单测和未来切换 provider。
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from config.settings import LLMSettings


def _resolve_disable_thinking(provider: str, model: str) -> dict[str, Any] | None:
    """根据 provider 和 model 返回关闭 thinking 模式的 extra_body 参数。

    不同 provider 关闭 thinking 的 API 参数各不相同：
    - qwen (qwen3 系列): ``{"enable_thinking": False}``
    - zhipu / minimax: ``{"thinking": {"type": "disabled"}}``
    - deepseek / openai 等: 不支持参数级关闭，返回 None。
    """
    if provider == "qwen" and model.startswith("qwen3"):
        return {"enable_thinking": False}
    if provider in ("zhipu", "minimax"):
        return {"thinking": {"type": "disabled"}}
    return None


def build_chat_model(
    llm_cfg: LLMSettings,
    *,
    disable_thinking: bool = False,
) -> ChatOpenAI:
    """根据配置构造 ChatOpenAI 兼容客户端。

    当 ``llm_cfg.use_system_proxy`` 为 False 时显式构造
    ``trust_env=False`` 的 httpx AsyncClient，强制直连，避免系统代理
    （HTTP_PROXY/HTTPS_PROXY）干扰。

    Args:
        llm_cfg: LLM 配置。
        disable_thinking: 是否关闭 thinking/reasoning 模式。
            仅对支持 thinking 的 provider + model 组合生效（qwen3 / zhipu / minimax）。
            用于 ReportAgent 等纯文本格式化场景，无需推理能力。
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
    if disable_thinking:
        extra_body = _resolve_disable_thinking(llm_cfg.provider, llm_cfg.model)
        if extra_body:
            kwargs["extra_body"] = extra_body
    return ChatOpenAI(**kwargs)
