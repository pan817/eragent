# 混合映射规则设计

## 1. 数据模型定义

```python
@dataclass
class GraphitiNode:
    """Graphiti 节点"""
    entity_type: str              # 节点类型，如 "PurchaseOrder"
    entity_id: str                # 唯一标识
    properties: dict[str, Any]    # 业务属性
    valid_from: datetime | None   # 业务生效时间
    valid_to: datetime | None     # 业务失效时间
    last_updated: datetime        # 最后修改时间


@dataclass
class GraphitiEdge:
    """Graphiti 边"""
    edge_type: str                # 关系类型，如 "CREATES_PO"
    source_type: str              # 起始节点类型
    source_id: str                # 起始节点 ID
    target_type: str              # 目标节点类型
    target_id: str                # 目标节点 ID
    created_at: datetime | None   # 关系建立时间
    properties: dict[str, Any] | None = None  # 边属性


@dataclass
class TextRecord:
    """待 LLM 抽取的自由文本记录"""
    source_node_type: str
    source_node_id: str
    field_name: str
    text: str


@dataclass
class TransformResult:
    """转换结果"""
    nodes: list[GraphitiNode]
    edges: list[GraphitiEdge]
    free_text_records: list[TextRecord]


@dataclass
class EdgeMapping:
    """边映射规则"""
    edge_type: str                                    # 关系类型
    source: tuple[str, str]                           # (节点类型, ID字段名)
    target: tuple[str, str]                           # (节点类型, ID字段名)
    timestamp_field: str | None = None                # 时间戳字段
    condition: Callable[[dict], bool] | None = None   # 条件函数（满足时才创建边）


@dataclass
class TableMapping:
    """表映射规则"""
    node_type: str                                    # 目标节点类型
    id_field: str                                     # 唯一标识字段
    property_mapping: dict[str, str | tuple[str, type]]  # {节点属性: EBS字段} 或 {节点属性: (EBS字段, 转换函数)}
    temporal: dict[str, str | None]                   # {valid_from: 字段, valid_to: 字段, last_updated: 字段}
    edges: list[EdgeMapping]                          # 边映射列表
    free_text_fields: list[str]                       # 需要 LLM 抽取的字段名
```

## 2. 映射注册表（代表性示例）

以下展示 3 张表的映射规则，其余 17 张表按相同模式定义。

### 2.1 PO_HEADERS_ALL

```python
"PO_HEADERS_ALL": TableMapping(
    node_type="PurchaseOrder",
    id_field="po_header_id",
    property_mapping={
        "po_number": "segment1",
        "type": "type_lookup_code",
        "status": "authorization_status",
        "total_amount": ("total_amount", float),
        "currency": "currency_code",
        "revision_num": "revision_num",
        "buyer_id": "agent_id",
        "closed_code": "closed_code",
        "comments": "comments",
    },
    temporal={
        "valid_from": "creation_date",
        "valid_to": None,
        "last_updated": "last_update_date",
    },
    edges=[
        EdgeMapping(
            edge_type="CREATES_PO",
            source=("Supplier", "vendor_id"),
            target=("PurchaseOrder", "po_header_id"),
            timestamp_field="creation_date",
        ),
    ],
    free_text_fields=["comments"],
),
```

### 2.2 AP_INVOICES_ALL

```python
"AP_INVOICES_ALL": TableMapping(
    node_type="Invoice",
    id_field="invoice_id",
    property_mapping={
        "invoice_num": "invoice_num",
        "invoice_type": "invoice_type_lookup_code",
        "invoice_amount": ("invoice_amount", float),
        "amount_paid": ("amount_paid", float),
        "currency": "invoice_currency_code",
        "approval_status": "approval_status",
        "gl_date": "gl_date",
        "description": "description",
    },
    temporal={
        "valid_from": "invoice_date",
        "valid_to": "cancelled_date",
        "last_updated": "last_update_date",
    },
    edges=[
        EdgeMapping(
            edge_type="SUBMITS_INVOICE",
            source=("Supplier", "vendor_id"),
            target=("Invoice", "invoice_id"),
            timestamp_field="invoice_date",
        ),
    ],
    free_text_fields=["description"],
),
```

### 2.3 OKC_K_HEADERS_B

```python
"OKC_K_HEADERS_B": TableMapping(
    node_type="Contract",
    id_field="id",
    property_mapping={
        "contract_number": "contract_number",
        "status": "sts_code",
        "estimated_amount": ("estimated_amount", float),
        "currency": "currency_code",
        "buy_or_sell": "buy_or_sell",
        "category": "scs_code",
        "description": "description",
        "short_description": "short_description",
    },
    temporal={
        "valid_from": "start_date",
        "valid_to": "end_date",
        "last_updated": "last_update_date",
    },
    edges=[
        # Supplier → Contract 关系在 PON_BID_HEADERS 授标时建立
    ],
    free_text_fields=["description", "short_description"],
),
```

> 完整的 20 张表映射在实现阶段逐表补齐，遵循相同的 `TableMapping` 结构。

## 3. LLM 抽取策略

### 3.1 需要 LLM 抽取的字段

| 表 | 字段 | 可能抽取的信息 |
|----|------|--------------|
| PO_HEADERS_ALL | comments | 特殊条款、紧急标记、审批备注 |
| AP_INVOICES_ALL | description | 争议原因、折扣说明 |
| RCV_SHIPMENT_HEADERS | comments | 运输异常、包装问题 |
| OKC_K_HEADERS_B | description | 合同关键条款、特殊约定 |
| OKC_K_HEADERS_B | short_description | 合同摘要 |

### 3.2 抽取流程

```python
class LLMTextExtractor:
    def __init__(self, llm, enabled: bool = True, batch_size: int = 20):
        self._llm = llm          # 使用 llm_fast 模型
        self._enabled = enabled   # 可通过配置开关
        self._batch_size = batch_size

    async def extract(self, records: list[TextRecord]) -> list[ExtraFact]:
        """
        批量 LLM 抽取。
        Prompt 模板：
          "从以下 ERP 备注中抽取关键业务事实：
           - 涉及的实体（供应商、物料、金额等）
           - 是否有异常/紧急/风险标记
           - 情感倾向（正面/中性/负面）
           返回 JSON 格式。"
        """
        if not self._enabled:
            return []
        # 按 batch_size 分批，减少 LLM 调用次数
        results = []
        for chunk in batched(records, self._batch_size):
            facts = await self._call_llm(chunk)
            results.extend(facts)
        return results
```

### 3.3 关键设计决策

1. **默认关闭**：`etl.llm_extraction_enabled` 默认 `false`，需显式开启
2. **使用 llm_fast**：自由文本抽取不需最强模型，控制成本
3. **批量处理**：累积 20 条一次调用，减少 API 调用次数
4. **追加不覆盖**：LLM 抽取结果作为附加事实追加到图谱，不覆盖结构化映射结果
5. **增量仅处理变更**：增量同步时只处理变更记录的自由文本，不重复已抽取内容
