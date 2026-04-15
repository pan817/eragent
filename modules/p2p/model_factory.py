"""LLM 客户端构造工厂。

把 ChatOpenAI 的构造从 P2PAgent 主类剥离，便于单测和未来切换 provider。
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from config.settings import LLMSettings
from core.logging_utils import get_logger

_logger = get_logger(__name__)


def _resolve_disable_thinking(provider: str, model: str) -> dict[str, Any] | None:
    """根据 provider 和 model 返回关闭 thinking 模式的 extra_body 参数。

    provider / model 统一按小写前缀匹配，兼容用户配置大小写差异及厂商子品牌命名：
    - provider 以 ``qwen`` 开头 **且** model 以 ``qwen3`` 开头:
      同时下发两种键名，覆盖两类后端（彼此对对方的键名静默忽略，不会 400）：
        * ``enable_thinking``：阿里云 DashScope / OpenAI 兼容模式识别
        * ``chat_template_kwargs.enable_thinking``：自建 vLLM / SGLang / LMDeploy
          等推理引擎按 chat template 参数识别
      仅 qwen3 系列文档化支持；对 ``qwen-max`` / ``qwen-plus`` 等非 thinking
      模型发送该参数可能被服务端拒绝，故保留 model 护栏。
    - provider 以 ``zhipu`` 或 ``minimax`` 开头: ``{"thinking": {"type": "disabled"}}``。
    - 其他组合（deepseek / openai / 未识别 provider 等）: 返回 None，不注入 extra_body。
    """
    provider_norm = provider.lower()
    model_norm = model.lower()

    if provider_norm.startswith("qwen") and model_norm.startswith("qwen3"):
        return {
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    if provider_norm.startswith("zhipu") or provider_norm.startswith("minimax"):
        return {"thinking": {"type": "disabled"}}

    _logger.info(
        "disable_thinking skipped: provider=%s model=%s (no matching rule)",
        provider,
        model,
    )
    return None


def build_chat_model(
    llm_cfg: LLMSettings,
    *,
    disable_thinking: bool = False,
    max_tokens_override: int | None = None,
) -> ChatOpenAI:
    """根据配置构造 ChatOpenAI 兼容客户端。

    当 ``llm_cfg.use_system_proxy`` 为 False 时显式构造
    ``trust_env=False`` 的 httpx AsyncClient，强制直连，避免系统代理
    （HTTP_PROXY/HTTPS_PROXY）干扰。

    Args:
        llm_cfg: LLM 配置。
        disable_thinking: 是否关闭 thinking/reasoning 模式。
            provider / model 匹配大小写不敏感且按前缀匹配，仅对支持参数级关闭的
            组合生效（qwen 系 + qwen3* 模型 / zhipu* / minimax*）。
            用于 ReportAgent 等纯文本格式化场景，无需推理能力。
        max_tokens_override: 覆盖 ``llm_cfg.max_tokens`` 的输出 token 硬上限。
            None 或 0 表示沿用 ``llm_cfg.max_tokens``。用于 ReportAgent 等
            对输出长度有专属限制的场景，避免直接修改共享的 LLMSettings。
    """
    import httpx

    max_tokens = (
        max_tokens_override if max_tokens_override else llm_cfg.max_tokens
    )
    kwargs: dict[str, Any] = dict(
        model=llm_cfg.model,
        api_key=llm_cfg.api_key,
        base_url=llm_cfg.api_base,
        temperature=llm_cfg.temperature,
        max_tokens=max_tokens,
        timeout=llm_cfg.timeout,
        # 显式开启流式能力。ChatOpenAI 即使 streaming=True，ainvoke 仍会
        # 一次性返回完整响应；只有调用方走 astream / astream_events 时才会
        # 真正触发 SSE 流式拉取。这里放宽开关，具体是否流式由调用方决定。
        streaming=llm_cfg.streaming_enabled,
    )
    if not llm_cfg.use_system_proxy:
        # 当前服务全程走 async 路径，仅创建 AsyncClient，避免空闲的同步连接池。
        # connect 阶段用短超时（5s），避免 DNS/TCP 握手被墙/卡住时空耗大量时间；
        # read 阶段使用 llm_cfg.timeout（通常数十秒~数分钟），LLM 输出本就耗时。
        timeout = httpx.Timeout(
            connect=5.0,
            read=float(llm_cfg.timeout),
            write=10.0,
            pool=5.0,
        )
        kwargs["http_async_client"] = httpx.AsyncClient(
            trust_env=False, timeout=timeout
        )
    if disable_thinking:
        extra_body = _resolve_disable_thinking(llm_cfg.provider, llm_cfg.model)
        if extra_body:
            kwargs["extra_body"] = extra_body
    return ChatOpenAI(**kwargs)
