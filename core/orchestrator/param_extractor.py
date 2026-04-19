"""前置 LLM 参数拆解器。

在意图路由之前调用 llm_fast，从用户 query 中提取结构化查询参数
（实体编号、时间范围、排序方式、数量限制），供所有路由路径（L1/L2/L3/
lookup shortcut）统一使用。

设计要点：
- 使用 llm_fast（关闭 thinking），单轮 JSON 输出，延迟可控
- 失败时返回空 dict，降级到纯 regex（与改动前行为一致）
- bypass 类查询（CHITCHAT/META/RECALL）不触发，由调用方控制
"""

from __future__ import annotations

import json
from typing import Any

from config.settings import Settings
from core.logging_utils import get_logger

_logger = get_logger(__name__)

# order_by 合法枚举值（用于校验 LLM 输出）
_VALID_ORDER_BY = frozenset({"date_desc", "date_asc", "amount_desc", "amount_asc"})

# 实体字段白名单（用于校验 LLM 输出）
_ENTITY_FIELDS = frozenset({
    "po_number", "supplier_id", "invoice_number",
    "payment_number", "receipt_number",
})

_EXTRACT_PROMPT = """你是 ERP 采购系统的参数提取器。从用户查询中提取结构化参数，输出纯 JSON。

## 规则
- 只提取用户查询中明确出现的具体值；代词或模糊引用（"上次那家"/"昨天的"）一律填 null
- 实体编号格式：PO-xxxx / SUP-xxxx / INV-xxxx / PAY-xxxx / RCV-xxxx（GR-xxxx 等同 RCV）
- days: 时间范围天数（"最近7天"→7，"本月"→30，"上周"→7），未指定填 null
- limit: 用户要求的数量限制（"一个"→1，"前5条"→5，"最新3个"→3），未指定填 null
- order_by: 排序方式，仅限以下枚举值之一：date_desc / date_asc / amount_desc / amount_asc
  * "最新"/"最近的" → date_desc
  * "最早"/"第一个" → date_asc
  * "金额最大"/"最贵" → amount_desc
  * "金额最小"/"最便宜" → amount_asc
  * 未指定排序 → null
- 当用户说"最新的N个"时，limit=N 且 order_by=date_desc

## 输出格式（纯 JSON，不要 markdown 代码块，第一个字符必须是 {{）
{{"po_number": null, "supplier_id": null, "invoice_number": null, "payment_number": null, "receipt_number": null, "days": null, "limit": null, "order_by": null}}

## 示例
- "查询最新的一个支付单" → {{"po_number": null, "supplier_id": null, "invoice_number": null, "payment_number": null, "receipt_number": null, "days": null, "limit": 1, "order_by": "date_desc"}}
- "查看 PO-2024-0001 的发票" → {{"po_number": "PO-2024-0001", "supplier_id": null, "invoice_number": null, "payment_number": null, "receipt_number": null, "days": null, "limit": null, "order_by": null}}
- "SUP-001 最近30天金额最大的5笔付款" → {{"po_number": null, "supplier_id": "SUP-001", "invoice_number": null, "payment_number": null, "receipt_number": null, "days": 30, "limit": 5, "order_by": "amount_desc"}}
- "最近7天的采购订单" → {{"po_number": null, "supplier_id": null, "invoice_number": null, "payment_number": null, "receipt_number": null, "days": 7, "limit": null, "order_by": null}}

用户查询：{query}"""


class ParamExtractor:
    """前置 LLM 参数拆解器。

    延迟初始化 LLM 客户端，首次调用时创建。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._llm: Any = None

    def _ensure_llm(self) -> Any:
        """延迟初始化 llm_fast 客户端（关闭 thinking）。"""
        if self._llm is not None:
            return self._llm

        from core.llm.model_factory import build_chat_model

        self._llm = build_chat_model(
            self._settings.llm_fast, disable_thinking=True
        )
        return self._llm

    def extract(self, query: str) -> dict[str, Any]:
        """从用户 query 中提取结构化参数。

        Args:
            query: 用户原始查询文本。

        Returns:
            提取的参数字典。失败时返回空 dict。
        """
        from core.observability.tracing import _safe_jsonable, estimate_tokens, record_span

        try:
            llm = self._ensure_llm()
            prompt = _EXTRACT_PROMPT.format(query=query)

            model_name = (
                getattr(llm, "model_name", None)
                or getattr(llm, "model", "unknown")
            )
            with record_span("model", f"param_extract:{model_name}") as model_attrs:
                model_attrs["model"] = str(model_name)
                model_attrs["input"] = prompt[:2000]
                model_attrs["estimated_input_tokens"] = estimate_tokens(prompt)
                response = llm.invoke(prompt)
                content: str = (
                    response.content if hasattr(response, "content") else str(response)
                )
                usage = (
                    getattr(response, "usage_metadata", None)
                    or getattr(response, "response_metadata", None)
                )
                model_attrs["output"] = {
                    "content": content[:2000],
                    "tool_calls": None,
                    "usage": _safe_jsonable(usage) if usage else None,
                }

            parsed = self._parse_response(content)
            _logger.info(
                "param extraction ok: query='%s' result=%s",
                query[:80], parsed,
            )
            return parsed

        except Exception as exc:
            _logger.warning(
                "param extraction failed, fallback to empty: %s", exc,
            )
            return {}

    def _parse_response(self, content: str) -> dict[str, Any]:
        """解析 LLM 返回的 JSON，校验并过滤字段。"""
        raw = content.strip()
        # 清理可能的 markdown 代码块
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("```").strip()

        data = json.loads(raw)
        if not isinstance(data, dict):
            _logger.warning("param extraction returned non-dict: %s", type(data))
            return {}

        result: dict[str, Any] = {}

        # 实体编号：只保留非空字符串
        for field in _ENTITY_FIELDS:
            val = data.get(field)
            if val and isinstance(val, str):
                result[field] = val.strip()

        # days：正整数
        days = data.get("days")
        if days is not None:
            try:
                days_int = int(days)
                if days_int > 0:
                    result["days"] = days_int
            except (TypeError, ValueError):
                pass

        # limit：正整数
        limit = data.get("limit")
        if limit is not None:
            try:
                limit_int = int(limit)
                if limit_int > 0:
                    result["limit"] = limit_int
            except (TypeError, ValueError):
                pass

        # order_by：枚举校验
        order_by = data.get("order_by")
        if order_by and isinstance(order_by, str) and order_by in _VALID_ORDER_BY:
            result["order_by"] = order_by

        return result
