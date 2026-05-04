# ERP Agent 架构重构设计

## 1. 概述

### 1.1 重构目标

当前项目功能已基本完善，但代码目录结构、逻辑分布和扩展性存在不足。本次重构**不新增功能**，仅优化代码结构和逻辑组织，为后续增强预留扩展性：

- **记忆功能增强**：记忆策略可插拔、多模态记忆支持
- **意图识别增强**：路由规则可配置、多模块意图共存
- **DAG 能力增强**：模板动态注册、工具自动发现
- **Tools 能力增强**：工具按职责拆分、跨模块复用
- **性能和健壮性增强**：分层解耦、依赖方向正确、关键路径可测试

### 1.2 重构原则

| 原则 | 说明 |
|------|------|
| 不增功能 | 重构前后功能行为完全一致，所有现有测试必须通过 |
| 生产标准 | 不引入临时方案或待重构标记，要么做完要么不做 |
| 分层正确 | 依赖方向：api → core → modules，禁止反向依赖 |
| 配置驱动 | 硬编码的业务规则、工具列表、模板映射迁移到配置或声明式注册 |
| 最小改动面 | 每个重构问题独立成 PR，可单独 review、测试、回滚 |

### 1.3 分析方法

通过对以下层次的代码进行逐文件审查，识别出 10 类架构问题：

- **API 层**：main.py、routes/、schemas/
- **Core 层**：orchestrator/、memory/、knowledge/、database/、tasks/、observability/
- **Modules 层**：modules/p2p/（agent、tools、rules、prompts、model_factory）
- **配置层**：config/settings.py、config.yaml
- **测试层**：tests/conftest.py、unit/、integration/

---

## 2. Schema 反向依赖（TECH-DEBT #2）

### 2.1 问题

`api/schemas/analysis.py` 定义的业务数据契约（`AnalysisRequest`、`AnalysisResult`、`Severity`、`AnomalyRecord`、`ErrorInfo`）被 core 层和 modules 层反向 import。依赖方向应为 api → core → modules，当前实际是 core/modules → api。

### 2.2 受影响文件（11 个）

**Core 层（6 个）：**
- `core/orchestrator/orchestrator.py:22`
- `core/orchestrator/router.py`
- `core/orchestrator/intent.py`
- `core/orchestrator/dag/templates.py`
- `core/tasks/registry.py:23`
- `core/tasks/schemas.py`

**Modules 层（5 个）：**
- `modules/p2p/agent.py:22`
- `modules/p2p/rules/three_way_match.py:15`
- `modules/p2p/rules/payment_compliance.py`
- `modules/p2p/rules/price_variance.py`
- `modules/p2p/rules/supplier_performance.py`

### 2.3 影响

- core/modules 无法脱离 FastAPI/api 层独立测试和复用
- SDK 打包时会被 FastAPI 依赖污染
- 违反"下层不依赖上层"的分层原则

### 2.4 方案

**不新建 `core/schemas/`**，避免两个 schemas 目录造成混淆（"该 import 哪个？"）。

当前 `api/schemas/analysis.py` 混杂了两类模型：
- **业务领域模型**：`AnalysisType`、`Severity`、`AnalysisStatus`、`KPIStatus`、`AnomalyRecord`、`AnomalyDetail`、`DocumentRef`、`AnalysisResult`、`ErrorInfo` — 被 core/modules 11 处引用，仅依赖 Pydantic
- **HTTP 接口模型**：`AnalysisRequest`（含 `output_mode`、`auto_persist`、`client_user_message_id` 等纯 HTTP/前端字段）— 仅被 api 层和 orchestrator 使用

拆为两个文件，保持在同一目录下：

```
api/schemas/
├── __init__.py
├── domain.py          # 业务领域模型（纯 Pydantic，不依赖 FastAPI）
├── analysis.py        # HTTP 接口模型：AnalysisRequest（依赖 domain.py）
├── session.py         # 不变
└── trace.py           # 不变
```

- `domain.py` 仅依赖 `pydantic`，不引入 FastAPI 耦合，core/modules 可安全 import
- 所有 11 个文件的 import 路径更新为 `from api.schemas.domain import ...`
- `analysis.py` 中 `AnalysisRequest` 通过 `from api.schemas.domain import AnalysisType` 引用领域模型
- 目录唯一，不产生"两个 schemas 该 import 哪个"的歧义

---

## 3. model_factory 层级错位

### 3.1 问题

`modules/p2p/model_factory.py` 是通用的 LLM 客户端工厂（构建 `ChatOpenAI` 实例、处理 HTTP 代理、reasoning 开关），与 P2P 业务无关，但被放置在 modules/p2p 下。

core 层的 `core/orchestrator/router.py:866` 直接 import 该模块，形成下层（core）依赖上层（modules）的违规。

### 3.2 调用方（3 处）

| 调用方 | 位置 | 层级 |
|--------|------|------|
| `core/orchestrator/router.py` | line 866 | core（违规） |
| `modules/p2p/agent.py` | line 33 | modules |
| `modules/p2p/report_agent.py` | line 85 | modules |

### 3.3 方案

将 `modules/p2p/model_factory.py` 移至 `core/llm/model_factory.py`：

```
core/llm/
├── __init__.py
└── model_factory.py    # build_chat_model()
```

- 更新 3 处 import 为 `from core.llm.model_factory import build_chat_model`
- 函数签名和行为不变，纯文件移动 + import 修正

---

## 4. Orchestrator 与 P2P 硬耦合

### 4.1 问题

`core/orchestrator/orchestrator.py`（1544 行）是核心编排器，但通过 lazy import 直接绑定 P2P 模块的 6 个具体实现：

| 位置 | import 内容 | 用途 |
|------|------------|------|
| line 351 | `modules.p2p.agent.P2PAgent` | ReAct 路径的 Agent |
| line 366 | `modules.p2p.report_agent.ReportAgent` | DAG 路径的报告生成 |
| line 500 | `modules.p2p.tools._get_repository` | 实体验证/补充时查询数据 |
| line 380-481 | `_validate_entities` 内嵌 | 实体 DB 验证（5 种实体硬编码，约 100 行） |
| line 484-605 | `_enrich_entities` 内嵌 | 实体级联补充（4 条补充规则硬编码，约 125 行） |
| line 674 | `modules.p2p.prompts.trim_to_token_budget` | 上下文裁剪 |
| line 703-795 | `_resolve_references` 内嵌的指代模式表 | 指代消解（5 组 P2P 实体正则，约 90 行） |
| line 1389 | `modules.p2p.agent._build_memory_content/metadata` | 长期记忆构建 |
| line 247-290 | `_get_checkpointer` / `_close_checkpointer` | 短期记忆 checkpointer 生命周期 |
| line 609-699 | `_load_session_context` | 短期记忆上下文读取 |
| line 1420-1480 | `_save_dag_to_short_term_memory` | DAG 路径短期记忆写入 |
| line 307-317 | `clear_short_term_memory` | 短期记忆清理 |

此外，实体类型在 line 1091-1095 硬编码（`po_number`、`supplier_id`、`invoice_number`、`payment_number`、`receipt_number`），route 决策逻辑与 P2P 实体强绑定。

### 4.2 影响

- 添加新业务模块（如 O2C）需修改 core 层代码
- Orchestrator 无法作为通用编排引擎复用
- 单元测试需 mock 大量 P2P 内部实现
- 实体处理三段逻辑（指代消解 90 行 + DB 验证 100 行 + 级联补充 125 行 = 315 行）内嵌在 Orchestrator 中，不属于编排职责
- 短期记忆（checkpointer 构建/读/写/清理）内嵌在 Orchestrator 中，不属于编排职责

### 4.3 方案

#### 4.3.1 统一 DAG 执行模型（核心设计决策）

**当前架构**：Orchestrator 内 `if use_dag` 二选一分支，DAG 和 ReAct 是平行的两条执行路径。

```
Orchestrator
├── use_dag=True  → _execute_dag()  → DAGExecutor(工具任务) → ReportAgent
└── use_dag=False → _execute_react() → P2PAgent(自主调工具)
```

**重构后**：统一走 DAG，P2PAgent 成为 DAG 可调度的一种任务类型。

```
Orchestrator → 统一入口 → DAGExecutor
                              ├── tool 类型节点：直接调工具（现有逻辑不变）
                              ├── report 类型节点：调 ReportAgent（现有逻辑不变）
                              └── agent 类型节点：调 P2PAgent ReAct（新增）
```

**三种场景映射：**

| 场景 | 当前路径 | 重构后 DAG 模板 |
|------|---------|----------------|
| 意图明确（L1/L2 命中） | `_execute_dag()` → 工具并行 → ReportAgent | 不变：tool 节点 + report 节点 |
| 意图模糊/回溯/数据查询 | `_execute_react()` → P2PAgent 自主 | 单节点 agent 模板：`[{type: agent, inputs: {query, session_id}}]` |
| 未来混合（本次不做） | 不支持 | tool 节点并行 + agent 子任务 + report 汇总 |

**收益：**
- 删除 `if use_dag` 分支，Orchestrator 只有一条执行路径
- 记忆写入统一：当前 `_save_dag_to_long/short_term_memory` 与 P2PAgent 内部 `save_memory` 各一套，合并为一处
- 可观测性统一：所有执行都经过 DAG span，trace 链路一致
- 不改业务逻辑：P2PAgent 内部的 ReAct 循环、工具调用、prompt 全部不变

#### 4.3.2 ModuleProvider Protocol

引入 `ModuleProvider` Protocol，模块实现此接口，Orchestrator 通过注册机制获取：

```python
# core/orchestrator/provider.py
class ModuleProvider(Protocol):
    """业务模块向 Orchestrator 提供的能力接口"""
    def get_agent(self, settings, ...) -> Any: ...
    def get_report_agent(self, settings, ...) -> Any: ...
    def get_repository(self) -> Any: ...
    def get_entity_types(self) -> list[str]: ...
    def get_tools(self) -> list[Callable]: ...
    def get_dag_templates(self) -> dict[AnalysisType, list[dict]]: ...
    def get_intent_rules(self) -> list[dict]: ...
    def get_reference_patterns(self) -> list[tuple[str, str, str]]: ...
    def get_enrichment_rules(self) -> list[dict]: ...
    def build_memory_content(self, result) -> str: ...
    def build_memory_metadata(self, result) -> dict: ...
    def trim_to_token_budget(self, text, budget) -> str: ...
```

```python
# modules/p2p/provider.py — P2P 模块实现
class P2PModuleProvider:
    def get_agent(self, settings, ...):
        from modules.p2p.agent import P2PAgent
        return P2PAgent(...)
    def get_entity_types(self):
        return ["po_number", "supplier_id", "invoice_number", "payment_number", "receipt_number"]
    # ...
```

#### 4.3.3 实体处理独立为 `core/orchestrator/entity.py`

当前 Orchestrator 内嵌了三段实体相关逻辑，共约 315 行，职责统一为"实体处理"——解析、验证、补充，不属于编排器：

| 逻辑 | 位置 | 行数 | 职责 |
|------|------|------|------|
| `_resolve_references` | line 703-795 | ~90 | 指代消解：将"这个PO"替换为具体实体 |
| `_validate_entities` | line 380-481 | ~100 | DB 验证：正则提取的实体是否在 DB 中存在 |
| `_enrich_entities` | line 484-605 | ~125 | 级联补充：payment→invoice→po→supplier |

三段逻辑是同一业务流水线（先解析→再验证→再补充），合并到一个文件：

```
core/orchestrator/
├── entity.py             # 实体处理：指代消解 + DB 验证 + 级联补充
├── orchestrator.py       # import entity.resolve_references / validate / enrich
└── ...
```

```python
# core/orchestrator/entity.py — 通用框架
def resolve_references(
    query: str,
    session_ctx: dict,
    patterns: list[tuple[str, str, str]],  # 由 ModuleProvider 提供
) -> tuple[str, dict]: ...

async def validate_entities(
    params: dict,
    repository: Any,                       # 由 ModuleProvider 提供
) -> None: ...

async def enrich_entities(
    params: dict,
    repository: Any,
    enrichment_rules: list[dict],          # 由 ModuleProvider 提供
) -> None: ...
```

```python
# modules/p2p/provider.py — P2P 提供实体处理规则
class P2PModuleProvider:
    def get_reference_patterns(self):
        return [
            (r"(?:这个|该|上述)\\s*(?:po|PO|订单|采购订单)", "po_number", "采购订单 {val}"),
            (r"(?:这个|该|上述)\\s*(?:供应商|vendor)", "supplier_id", "供应商 {val}"),
            ...
        ]
    def get_enrichment_rules(self):
        return [
            {"source": "payment_number", "targets": ["invoice_number"]},
            {"source": "receipt_number", "targets": ["po_number", "supplier_id"]},
            {"source": "invoice_number", "targets": ["po_number", "supplier_id"]},
            {"source": "po_number", "targets": ["supplier_id"]},
        ]
```

#### 4.3.4 短期记忆提取到 `core/memory/short_term.py`

当前 Orchestrator 承担了短期记忆的 checkpointer 生命周期管理、上下文读取、写入、清理（4 段逻辑，约 200 行），这些不属于编排职责。

提取到 `core/memory/short_term.py`，封装为 `ShortTermMemory` 类：

```python
# core/memory/short_term.py
class ShortTermMemory:
    """基于 LangGraph PostgresSaver 的短期记忆管理。"""
    def __init__(self, conninfo: str, ...): ...
    def load_session_context(self, session_id: str) -> dict: ...
    def save(self, query: str, response: str, session_id: str, ...) -> None: ...
    def clear(self, session_id: str) -> int: ...
    def close(self) -> None: ...
```

Orchestrator 通过 `core.memory` 统一 import 使用：

```python
from core.memory import ShortTermMemory
```

配合统一 DAG 模型（4.3.1），记忆写入不再分散在 `_execute_dag` / `_execute_react` 两处，由 Orchestrator 在 DAG 执行完成后统一调用 `short_term.save()`。

#### 4.3.5 Orchestrator 改造要点

- Orchestrator 构造时接收 `ModuleProvider` 实例，不再直接 import 模块代码
- 删除 `_execute_react()` 方法，其逻辑提取为可被 DAGExecutor 调用的独立函数
- 删除 `if use_dag` 分支，路由决策结果统一映射到 DAG 模板选择
- 实体处理（指代消解+验证+补充）提取到 `core/orchestrator/entity.py`，规则从 Provider 获取
- 短期记忆操作提取到 `core/memory/short_term.py`，Orchestrator 不再管理 checkpointer
- 实体类型列表由 Provider 声明，route 决策逻辑根据列表动态判断
- `api/main.py` 在 lifespan 中装配 Provider → Orchestrator

---

## 5. 配置层单体化

### 5.1 问题

`config/settings.py` 是全局配置中心，但混入了 7 个 P2P 专属配置类：

| 配置类 | 职责 | 行号范围 |
|--------|------|---------|
| `ThreeWayMatchSettings` | 三路匹配容差策略 | settings.py |
| `PaymentComplianceSettings` | 付款合规规则 | settings.py |
| `SupplierPerformanceBenchmarks` | 供应商绩效基准 | settings.py |
| `AnomalySeveritySettings` | 异常严重等级阈值 | settings.py |
| `OntologyContextSettings` | 本体上下文 token 预算 | settings.py |
| `ToolOutputSettings` | 工具输出裁剪限制 | settings.py |
| `P2PSettings` | 以上 6 项的聚合入口 | settings.py |

此外，`ChromaSettings` 中硬编码了 P2P 集合名（`ontology_p2p`、`business_docs_p2p`）。

### 5.2 影响

- 添加新模块必须修改全局 settings.py，造成合并冲突
- settings.py 职责过重，P2P 业务规则与基础设施配置混杂
- 无法按模块独立加载配置

### 5.3 方案

**分离 core 配置与模块配置：**

```
config/
├── settings.py          # 仅保留通用配置：LLMSettings, DatabaseSettings,
│                        #   ChromaSettings(去掉P2P集合名), Neo4jSettings,
│                        #   MemorySettings, ObservabilitySettings, AppSettings
├── config.yaml          # 通用配置值
└── crypto.py            # 不变

modules/p2p/
├── settings.py          # P2P 专属配置：ThreeWayMatchSettings, PaymentComplianceSettings,
│                        #   SupplierPerformanceBenchmarks, AnomalySeveritySettings,
│                        #   OntologyContextSettings, ToolOutputSettings, P2PSettings
└── ...
```

- `config/settings.py` 的 `Settings` 类不再包含 `p2p: P2PSettings` 字段
- P2P 模块通过自己的 `modules/p2p/settings.py` 加载配置，可从 `config.yaml` 的 `p2p:` 段或环境变量读取
- Chroma 集合名由模块在注册时声明，`ChromaSettings` 只保留连接参数

---

## 6. DAG 子系统硬编码

### 6.1 问题

`core/orchestrator/dag/` 下 4 个文件均硬编码 P2P 工具和模板：

**registry.py（line 54-102）：** `build_default_registry()` 显式 import 19 个 P2P 工具函数，逐个 `registry.register(name, fn)` 注册。添加新工具需修改此文件。

**templates.py（791 行）：** 11 个静态 DAG 模板全部硬编码 P2P 工具名（如 `query_purchase_orders`、`run_three_way_match`）和 agent 名（`p2p_agent` 出现 100+ 次）。`_TEMPLATE_MAP`（line 716-727）硬编码 AnalysisType → 模板映射。

**validator.py（line 15-29）：** 硬编码 3 组工具分类：
- `_DATA_TOOLS`：8 个数据查询工具
- `_ANALYSIS_TOOLS`：9 个分析工具
- `_REPORT_TOOLS`：2 个报告工具

**executor.py（line 24）：** 硬编码 `_REPORT_TOOLS = {"generate_summary_report", "generate_chart"}`。

### 6.2 影响

- 添加一个新分析类型需修改 6-8 个文件（约 50 行代码散落在 registry、templates、validator、router 中）
- 工具分类无元数据，全靠人工维护三个集合的一致性
- DAG 模板不可配置，无法运行时动态加载

### 6.3 方案

**6.3.1 DAGExecutor 支持三种任务类型**

配合第 4 章统一 DAG 执行模型，DAGExecutor 的 `run_task()` 扩展为三种任务类型：

```python
# task 定义中通过 type 字段区分
{
    "task_id": "t1",
    "type": "tool",       # tool | report | agent
    "tool_name": "...",   # type=tool 时必填
    "depends_on": [],
    "inputs": {},
    "timeout_sec": 780,
}
```

| type | 执行逻辑 | 输出 |
|------|---------|------|
| `tool` | 从 ToolRegistry 获取工具函数并调用（现有逻辑不变） | 工具返回的 JSON/文本 |
| `report` | 调用 ReportAgent 汇总（现有逻辑不变） | Markdown 报告 |
| `agent` | 调用 ModuleProvider 提供的 Agent 执行 ReAct（新增） | Agent 最终输出 |

**agent 类型节点的关键设计：**
- 通过 `inputs` 接收 `query`、`session_id`、`context_summary` 等上下文
- 超时时间独立配置（agent 任务通常比工具任务耗时更长）
- 输出直接作为最终结果或作为后续 report 节点的输入
- Agent 实例从 ModuleProvider 获取，DAGExecutor 不直接 import 模块代码

**ReAct 场景的模板示例：**
```python
# 意图模糊 / 回溯 / 数据查询 → 单节点 agent 模板
_REACT_FALLBACK_DAG = [
    {
        "task_id": "react",
        "type": "agent",
        "inputs": {"query": "{query}", "session_id": "{session_id}"},
        "depends_on": [],
        "timeout_sec": 900,
        "output_key": "report",
    },
]
```

**6.3.2 工具自注册 + 元数据驱动**

工具定义时声明 category 元数据，registry 自动从模块收集：

```python
# modules/p2p/tools/query.py
@tool(metadata={"category": "data", "module": "p2p"})
def query_purchase_orders(...): ...
```

`ToolRegistry` 改为从 `ModuleProvider.get_tools()` 自动注册，不再硬编码 import。

**6.3.3 模板配置化**

DAG 模板从 Python dict 迁移到模块声明：

```python
# modules/p2p/dag_templates.py — 模块自带模板定义
TEMPLATES = {
    AnalysisType.THREE_WAY_MATCH: [
        {"task_id": "t1", "type": "tool", "tool_name": "query_purchase_orders", "depends_on": []},
        ...
    ],
    ...
}
```

`ModuleProvider.get_dag_templates()` 返回模板字典，core 层的 `templates.py` 改为聚合各模块模板。

**6.3.4 Validator 从元数据派生**

`DAGValidator` 不再维护硬编码工具集合，改为从 `ToolRegistry` 的 category 元数据自动推导：

```python
data_tools = registry.get_tools_by_category("data")
analysis_tools = registry.get_tools_by_category("analysis")
report_tools = registry.get_tools_by_category("report")
```

同时新增 `agent` 类型的校验规则（如：agent 节点不可被其他 agent 节点依赖）。

---

## 7. IntentRouter 扩展性不足

### 7.1 问题

`core/orchestrator/router.py`（1114 行）承担了过多职责，且核心数据全部硬编码：

**职责过载（6 项）：**
1. L1 关键词匹配逻辑
2. L2 Chroma 语义匹配逻辑
3. L3 LLM 分类逻辑
4. Prompt 管理（L3 分类 prompt 模板）
5. 角色映射（5 种分析师角色）
6. Seed 加载与 Chroma 同步

**硬编码清单：**

| 数据 | 位置 | 数量 |
|------|------|------|
| `_RULE_LIBRARY` L1 关键词规则 | line 258-339 | 10 条规则 |
| `_ROLE_DESCRIPTIONS` 角色描述 | line 50-72 | 5 种角色 |
| `_LLM_CLASSIFY_PROMPT` L3 prompt | line 802-854 | 硬编码 11 种 intent_kind + 11 种 analysis_type |
| L2 相似度阈值 | line 734 | 0.80 |
| L2 长度比折扣 | line 178-179 | 0.4 / 0.7 |

### 7.2 影响

- 添加新分析类型需同时修改 `_RULE_LIBRARY`、L3 prompt 文本、`intent_seeds.yaml`
- 角色扩展需修改 core 层代码
- router.py 单文件 1114 行，难以独立测试各层级

### 7.3 方案

**7.3.1 Router 拆分为策略模式**

```
core/orchestrator/router/
├── __init__.py          # IntentRouter（组合三层策略）
├── l1_keyword.py        # L1Strategy：关键词匹配
├── l2_semantic.py       # L2Strategy：Chroma 语义检索
├── l3_llm.py            # L3Strategy：LLM 分类
└── signal.py            # QuerySignal / IntentKind（不变）
```

每层策略实现统一接口 `def classify(query, context) -> QuerySignal | None`，IntentRouter 按 L1 → L2 → L3 串行尝试。

**7.3.2 规则库配置化**

L1 关键词规则从代码搬到配置，模块可注册自己的规则：

```python
# ModuleProvider.get_intent_rules() -> list[IntentRule]
# 模块声明自己的关键词规则，Router 启动时聚合
```

**7.3.3 L3 Prompt 模板化**

L3 分类 prompt 不再硬编码 analysis_type 列表，改为从已注册的模块动态生成：

```python
analysis_types = module_provider.get_analysis_types()  # 动态获取
prompt = CLASSIFY_TEMPLATE.format(types="\n".join(analysis_types))
```

**7.3.4 阈值配置化**

L2 相似度阈值（0.80）、长度比折扣（0.4/0.7）等参数迁入 `config.yaml` 的 `intent_router:` 配置段。

---

## 8. Knowledge Graph P2P 实体硬编码

### 8.1 问题

`core/knowledge/graph.py`（line 156-491）定义了 5 个 P2P 专属节点方法和硬编码的 schema 映射：

**硬编码方法：**
- `create_supplier_node()`（line 201-215）
- `create_po_node()`（line 217-232）
- `create_invoice_node()`（line 234-249）
- `create_receipt_node()`（line 251-266）
- `create_payment_node()`（line 268-283）

**硬编码映射（line 456-463）：**
```python
id_field_mapping = {
    "Supplier": "supplier_id",
    "PurchaseOrder": "po_number",
    "Invoice": "invoice_id",
    "Payment": "payment_id",
}
```

### 8.2 影响

- 新模块的实体类型（如 O2C 的 SalesOrder、Delivery）无法使用现有 graph 层
- 添加新实体需修改 core 层代码

### 8.3 方案

将 P2P 专属方法从 `KnowledgeGraph` 类中提取，保留通用 CRUD 接口：

```python
# core/knowledge/graph.py — 通用接口
class KnowledgeGraph:
    def create_node(self, label: str, properties: dict) -> str: ...
    def create_relationship(self, from_id: str, to_id: str, rel_type: str, properties: dict) -> None: ...
    def query_nodes(self, label: str, filters: dict) -> list[dict]: ...
```

P2P 专属的节点创建逻辑（字段校验、默认值设置）移至模块层：

```python
# modules/p2p/graph_schema.py
class P2PGraphSchema:
    """P2P 实体在 Neo4j 中的 schema 定义和便捷方法"""
    def create_supplier(self, graph: KnowledgeGraph, data: dict) -> str:
        return graph.create_node("Supplier", {
            "supplier_id": data["supplier_id"],
            "name": data["name"],
            ...
        })
```

`id_field_mapping` 由模块通过 `ModuleProvider.get_graph_schema()` 声明。

---

## 9. Prompt 分文件管理

### 9.1 问题

当前 prompt 文本散落在各 agent/router 的实现文件中，与调度逻辑混杂：

| prompt | 当前位置 | 行数 | 问题 |
|--------|---------|------|------|
| P2P Agent system prompt | `modules/p2p/prompts.py:167-216` | ~50 | 已独立（唯一合理的） |
| ReportAgent 报告生成 prompt | `modules/p2p/report_agent.py:35-66` | ~30 | 内嵌在 agent 类文件中 |
| IntentRouter L3 分类 prompt | `core/orchestrator/router.py:802-854` | ~50 | 内嵌在路由逻辑中 |
| output_mode 格式指令 | `core/orchestrator/orchestrator.py:53-100` | ~50 | 内嵌在编排逻辑中 |
| 早退响应模板 | `core/orchestrator/orchestrator.py:138-215` | ~80 | 内嵌在编排逻辑中（含 CLARIFICATION/META/CHITCHAT/OUT_OF_SCOPE 4 种模板 + P2P 场景文案） |

### 9.2 影响

- prompt 修改需要在逻辑文件中定位，容易误改周围代码
- 无法一目了然看到某个组件使用的全部 prompt
- prompt review（措辞调优、多语言适配）需要打开逻辑文件

### 9.3 方案

**原则：prompt 文本 + 渲染函数放 prompts.py，agent/router 逻辑文件只 import 调用。**

所有 prompt 都包含动态变量（f-string / `.format()`），不适合拆为 .txt 纯文本文件，保持 .py 文件即可。

**归属规则：谁的 prompt 放谁的 prompts.py。**

| prompt | 重构后位置 | 说明 |
|--------|-----------|------|
| P2P Agent system prompt | `modules/p2p/prompts.py` | 不变 |
| ReportAgent 报告生成 prompt | `modules/p2p/prompts.py` | 与 P2P system prompt 同属 P2P 模块，合入同一文件 |
| IntentRouter L3 分类 prompt | `core/orchestrator/router/prompts.py` | 随 router 拆分为包（第 7 章），prompt 独立为文件 |
| output_mode 格式指令 | `core/orchestrator/prompts.py` | 从 orchestrator.py 提取，编排层 prompt 集中管理 |
| 早退响应模板 | `core/orchestrator/prompts.py` | 从 orchestrator.py 提取，与 output_mode 同属编排层 prompt |

**重构后结构：**

```
core/orchestrator/
├── prompts.py               # output_mode 格式指令 + 渲染函数
├── orchestrator.py           # import prompts.build_output_mode_prompts()
├── router/
│   ├── prompts.py            # L3 分类 prompt 模板 + 渲染函数
│   ├── l3_llm.py             # import prompts.render_classify_prompt()
│   └── ...

modules/p2p/
├── prompts.py                # P2P Agent system prompt + ReportAgent prompt + 渲染函数
├── agent.py                  # import prompts.build_system_prompt()
├── report_agent.py           # import prompts.build_report_prompt()
└── ...
```

- 每个 prompts.py 只包含 prompt 常量和渲染函数，不包含业务逻辑
- agent/router 文件通过 import 获取渲染好的 prompt 字符串

---

## 10. tools.py 单体文件

### 10.1 问题

`modules/p2p/tools.py`（1058 行）包含 19 个 `@tool` 函数，外加全局 `_repository` 单例注入（line 34-49）和 `_clip_and_dump()` 输出裁剪逻辑（line 52-139）。

**现有分组（按注释块）：**
- 查询工具（4 个）：`query_purchase_orders`、`query_receipts`、`query_invoices`、`query_payments`
- 分析工具（5 个）：`run_three_way_match`、`run_price_variance_analysis`、`run_payment_compliance_check`、`calculate_supplier_kpis`、`query_vendor_master`
- 高级分析工具（5 个）：`analyze_receipt_anomalies`、`detect_duplicate_invoices`、`analyze_discount_utilization`、`analyze_vendor_concentration`、`calculate_spend_analysis`
- 预留工具（5 个）：`query_material_master`、`calculate_po_cycle_time`、`run_vendor_risk_scoring`、`check_approval_limits`、`check_blacklist`

### 10.2 影响

- 单文件过大，代码导航和 review 困难
- `_repository` 全局单例是模块级状态，不利于测试隔离
- `_clip_and_dump()` 是通用输出裁剪逻辑，不属于 P2P 业务

### 10.3 方案

**10.3.1 按职责拆分为子模块**

```
modules/p2p/tools/
├── __init__.py            # 统一导出所有工具 + set_repository()
├── _output.py             # _clip_and_dump() 输出裁剪（P2P 工具专属，读 p2p.tool_output 配置）
├── _inject.py             # _repository 注入管理（get/set）
├── query.py               # 4 个数据查询工具
├── analysis.py            # 5 个规则分析工具
├── advanced.py            # 5 个高级分析工具
└── stub.py                # 5 个预留工具
```

**10.3.2 输出裁剪保留在模块内**

`_clip_and_dump()` 读取 `settings.p2p.tool_output` 配置，裁剪提示信息也包含 P2P 语境，是 P2P 工具的私有输出格式化函数。保留在 `modules/p2p/tools/_output.py`，不提升到 core 层。未来若其他模块需要类似能力，再提取通用接口。

**10.3.3 Repository 注入改进**

`_repository` 全局变量改为通过 `_inject.py` 集中管理，保持 `set_repository()` / `_get_repository()` 接口不变，但后续可演进为依赖注入容器。

---

## 11. Database Repository 不可插拔

### 11.1 问题

`core/database/repository.py` 定义了 `P2PRepository`，包含 4 个 P2P 专属查询方法（`query_purchase_orders()`、`query_receipts()`、`query_invoices()`、`query_payments()`）。

`core/database/__init__.py` 直接导出 `P2PRepository`：
```python
from core.database.repository import P2PRepository
__all__ = ["Base", "P2PRepository", ...]
```

`api/main.py:136` 在启动时硬编码创建 `P2PRepository` 实例并注入。

### 11.2 影响

- core 层包含 P2P 业务查询逻辑，违反分层
- 添加新模块需在 core/database/ 添加新 Repository 类
- API 启动逻辑需为每个模块硬编码初始化

### 11.3 方案

**11.3.1 Repository 下沉到模块**

将 `P2PRepository` 从 `core/database/repository.py` 移至 `modules/p2p/repository.py`。

`core/database/repository.py` 只保留通用基类（如已有的通用 CRUD 方法），不包含任何业务查询。

```
core/database/
├── engine.py           # engine / session 工厂（不变）
├── models.py           # Base（不变）
├── repository.py       # 通用 Repository 基类（去掉 P2P 方法）
└── init_db.py          # 建表入口（不变）

modules/p2p/
├── repository.py       # P2PRepository（从 core 迁入）
└── ...
```

**11.3.2 API 启动解耦**

`api/main.py` 不再硬编码 `P2PRepository` 初始化，改为通过 `ModuleProvider`（第 4 章）获取 repository：

```python
# api/main.py lifespan
provider = P2PModuleProvider(session_factory)
orchestrator = Orchestrator(provider=provider, ...)
```

---

## 12. 测试 Fixture 耦合

### 12.1 问题

`tests/conftest.py` 是唯一的 fixture 文件，混入了基础设施 fixture 和 P2P 业务数据 fixture：

**基础设施 fixture（应保留在根 conftest）：**
- `settings`、`p2p_settings`、`db_engine`、`db_session_factory`、`repository`

**P2P 业务 fixture（应下沉到模块测试目录）：**
- `mock_po_data`、`mock_gr_data`、`mock_invoice_data`、`mock_payment_data`

### 12.2 影响

- 所有测试加载时都会初始化 P2P mock 数据（即使测试与 P2P 无关）
- 添加新模块的 mock 数据继续堆积在根 conftest 中
- fixture 依赖关系不清晰

### 12.3 方案

按层级拆分 conftest：

```
tests/
├── conftest.py              # 仅保留基础设施：db_engine, session, settings
├── unit/
│   ├── conftest.py          # 单元测试公共 fixture（如 mock LLM）
│   ├── test_rules.py        # P2P 规则测试（使用下方 p2p fixture）
│   └── ...
└── fixtures/
    └── p2p.py               # P2P mock 数据：mock_po_data, mock_gr_data 等
                             # 通过 pytest_plugins 或 conftest import 引入
```

- 根 conftest 只提供 `db_engine`、`db_session_factory`、`settings` 等基础设施
- P2P 测试数据移至 `tests/fixtures/p2p.py`，在需要的 conftest 中通过 `pytest_plugins` 引入
- `p2p_settings` fixture 随 P2P 配置拆分（第 5 章）自然下沉

---

## 13. `__init__.py` 导出规范

### 13.1 问题

各 core 子包的 `__init__.py` 导出策略不一致：

| 包 | 现状 | 问题 |
|------|------|------|
| `core/memory/__init__.py` | 导出 Repo 类 + 单例函数 + table 定义 | 混杂了内部表定义 |
| `core/chat/__init__.py` | 导出 ChatRepository + 单例函数，**不导出** table | 消费方需 `from core.chat.tables import ...` |
| `core/observability/__init__.py` | 仅导出 `TimingMiddleware` | 消费方需直接 import 内部文件 |
| `core/tasks/__init__.py` | 导出 Registry + EventBus 单例函数 | 不导出 `events_redis.py`（合理，但没有统一规则） |
| `core/database/__init__.py` | 导出 P2PRepository（待迁走） | 迁走后需重新定义导出 |

### 13.2 影响

- 开发者不知道应该 `from core.X import Y` 还是 `from core.X.internal import Y`
- 重构时不清楚哪些是公共 API、哪些是内部实现

### 13.3 方案

重构时统一每个 core 子包的 `__init__.py` 导出策略：

**规则：`__init__.py` 只导出该包的公共 API（类、工厂函数、单例访问器），不导出 table 定义和内部工具函数。**

| 包 | 应导出 | 不应导出 |
|------|--------|---------|
| `core/memory/` | `LongTermMemory`, `ShortTermMemory`, `get_long_term_memory()` 等 | `memories_table`, `metadata_obj`（仅 migrations 和 init_db 需要） |
| `core/chat/` | `ChatRepository`, `get_chat_repository()`, `init_chat_repository()` | `chat_sessions_table`, `chat_messages_table` |
| `core/observability/` | `TimingMiddleware`, `record_span`, `estimate_tokens` | `TraceRun`, `TraceSpan`（仅 store/migrations 需要） |
| `core/tasks/` | `init_event_bus`, `init_task_registry`, `shutdown_*` 等 | `RedisEventBus`（内部实现） |
| `core/database/` | `get_engine`, `get_session_factory`, `create_tables`, `Base` | `P2PRepository`（迁走后不再导出） |

- table 定义由 `migrations/env.py` 和 `core/database/init_db.py` 直接 import 内部文件，不通过 `__init__.py`
- 每个 `__init__.py` 顶部注释标注"公共 API"，明确导出边界

---

## 14. 重构优先级与分阶段路线图

### 14.1 优先级排序原则

| 维度 | 说明 |
|------|------|
| 风险 | 改动影响面越小越优先 |
| 收益 | 解除的耦合点越多越优先 |
| 依赖 | 被其它重构依赖的任务优先 |

### 14.2 全链路 Trace Span 覆盖要求

重构后，analyze 接口的每个业务阶段必须有对应的 `record_span`，确保全链路可观测。以下是 span 覆盖表——当前已有的必须保留，标注"缺失"的必须在重构中补齐。

| 阶段 | span_type | span_name | 重构后所在文件 | 当前状态 |
|------|-----------|-----------|--------------|---------|
| 短期记忆读取 | `checkpoint` | `load_session_context` | `core/memory/short_term.py` | ✅ 已有 |
| 指代消解 | `entity` | `resolve_references` | `core/orchestrator/entity.py` | ❌ 缺失，重构时补齐 |
| 意图路由（总） | `intent` | `route_decision` | `core/orchestrator/router/__init__.py` | ✅ 已有 |
| 意图路由 L1 | `intent.l1` | `keyword_match` | `core/orchestrator/router/l1_keyword.py` | ✅ 已有 |
| 意图路由 L2 | `intent.l2` | `semantic_search` | `core/orchestrator/router/l2_semantic.py` | ✅ 已有 |
| 意图路由 L3 | `intent.l3` | `llm_classify` | `core/orchestrator/router/l3_llm.py` | ✅ 已有 |
| L3 LLM 调用 | `model` | `{model_name}` | `core/orchestrator/router/l3_llm.py` | ✅ 已有 |
| 实体 DB 验证 | `entity` | `validate_entities` | `core/orchestrator/entity.py` | ✅ 已有 |
| 实体级联补充 | `entity` | `enrich_entities` | `core/orchestrator/entity.py` | ✅ 已有 |
| output_mode 解析 | `orchestrator` | `resolve_output_mode` | `core/orchestrator/prompts.py` | ❌ 缺失，重构时补齐 |
| DAG 执行（总） | `dag` | `dag_execution` | `core/orchestrator/dag/executor.py` | ✅ 已有 |
| DAG 单任务 | `dag.task` | `{task_id}:{tool_name}` | `core/orchestrator/dag/executor.py` | ✅ 已有 |
| DAG 工具调用 | `tool` | `{tool_name}` | `core/orchestrator/dag/executor.py` | ✅ 已有 |
| Agent 任务（新增） | `dag.task` | `{task_id}:agent` | `core/orchestrator/dag/executor.py` | 🆕 随 agent 任务类型新增 |
| 报告生成 | `report` | `generate_report` | `modules/p2p/report_agent.py` | ✅ 已有 |
| context_budget | `context_budget` | `context_budget` | `modules/p2p/agent.py` | ✅ 已有 |
| 记忆裁剪 | `memory_trim` | `memory_trim` | `core/memory/trimmer.py` | ✅ 已有 |
| 短期记忆写入 | `checkpoint` | `dag_short_term_write` | `core/memory/short_term.py` | ✅ 已有 |
| 长期记忆写入 | `memory` | `save_memory` | `core/memory/long_term.py` | ✅ 已有 |
| 报告持久化 | `memory` | `persist_report` | `core/memory/long_term.py` | ✅ 已有 |
| DAG 案例存储 | `case_store` | `store_dag_case` | `core/orchestrator/dag/case_store.py` | ✅ 已有 |

**验收标准**：重构后运行一次完整的 analyze 请求（DAG 路径 + ReAct 路径各一次），通过 `GET /traces` 查询，确认上表所有 span 均出现在 trace 树中。缺失任何一个 span 视为重构未完成。

### 14.3 开关治理速查表

当前项目有 16 个布尔开关，分布在 5 个配置类中。本次重构不改开关代码，但需在 `config.yaml` 中按用途分组注释，便于运维快速定位。

#### 开关分级

| 级别 | 含义 | 使用场景 |
|------|------|---------|
| **L1 紧急降级** | 生产故障时一键关闭某能力 | oncall 值班操作 |
| **L2 功能开关** | 按需启停整个子系统 | 部署配置 |
| **L3 策略调参** | 裁剪策略、过滤策略的微调 | 性能调优 |
| **L4 调试开关** | 开发/排障专用的日志详细度控制 | 本地开发、线上排障 |

#### 完整开关清单

| 开关 | 所属配置 | 默认值 | 级别 | 用途 |
|------|---------|--------|------|------|
| `llm.streaming_enabled` | LLMSettings | `true` | **L1** | LLM 流式输出降级开关，关闭后回退为 ainvoke |
| `neo4j.enabled` | Neo4jSettings | `false` | **L2** | Neo4j 图数据库总开关 |
| `memory.long_term_enabled` | MemorySettings | `true` | **L2** | 长期记忆总开关 |
| `app.tiktoken_warmup_enabled` | Settings | `true` | **L2** | 启动期 tiktoken BPE 编码预热 |
| `memory.react_trim_enabled` | MemorySettings | `true` | **L3** | ReAct 循环内 LLM 输入裁剪总开关 |
| `memory.short_term_context_trim_enabled` | MemorySettings | `true` | **L3** | 短期记忆注入 LLM 前裁剪 |
| `memory.long_term_context_trim_enabled` | MemorySettings | `true` | **L3** | 长期记忆注入 LLM 前裁剪 |
| `p2p.ontology.context_trim_enabled` | OntologyContextSettings | `true` | **L3** | 本体上下文注入 LLM 前裁剪 |
| `memory.long_term_skip_empty_conclusions` | MemorySettings | `false` | **L3** | 跳过无异常且 summary 为空的分析结论 |
| `observability.console_enabled` | ObservabilitySettings | `true` | **L4** | 控制台打印 trace 树 + 汇总 |
| `observability.console_io_panel` | ObservabilitySettings | `false` | **L4** | 每次 model/tool 调用的 I/O 面板 |
| `observability.verbose_calls` | ObservabilitySettings | `true` | **L4** | 高频调用（LLM/Tool/Memory）INFO 日志 |
| `observability.log_llm_content` | ObservabilitySettings | `true` | **L4** | LLM prompt/response 内容日志 |
| `observability.log_on_error_only` | ObservabilitySettings | `false` | **L4** | 仅异常时打印内容（正常路径静默） |
| `logging.include_trace_id` | LoggingSettings | `true` | **L4** | 日志中注入 trace_id 列 |
| `llm.use_system_proxy` | LLMSettings | `false` | **L4** | HTTP 代理开关（调试网络问题用） |

#### 重构要求

- `config.yaml` 中按 L1 → L4 分组排列开关，加注释说明级别和用途
- 重构过程中如果开关的检查逻辑随代码迁移（如 `context_trim_enabled` 从 orchestrator.py 移到 entity.py），必须保持开关行为不变
- 不合并或删减开关（属于功能增强，不在本次范围）

### 14.4 分阶段计划

#### Phase 1：文件移动与改名（零逻辑变更）

本阶段只做文件移动、改名、import 路径修正，不改任何业务逻辑。

| 序号 | 任务 | 对应章节 | 改动面 |
|------|------|---------|--------|
| 1.1 | `api/schemas/analysis.py` 拆为 `domain.py` + `analysis.py` | 第 2 章 | 拆文件 + 11 处 import |
| 1.2 | `modules/p2p/model_factory.py` → `core/llm/model_factory.py` | 第 3 章 | 移文件 + 3 处 import |
| 1.3 | `core/memory/middleware.py` → `core/memory/trimmer.py` | — | 改名 + 3 处 import |
| 1.4 | `core/observability/middleware.py` → `tracing.py` + `streaming.py` | — | 拆文件 + 60+ 处 import |

**测试验证**：
- 每项任务完成后运行 `pytest`，全量通过
- Phase 结束时验证：`grep -r "from core.observability.middleware" --include="*.py"` 无结果
- Phase 结束时验证：`grep -r "from modules.p2p.model_factory" --include="*.py"` 无结果

**产出**：文件位置和命名修正完毕，依赖方向合规。

#### Phase 2：P2P 业务代码下沉（core 层净化）

将 core 层中的 P2P 专属代码迁移到 modules/p2p，core 层变为通用框架。

| 序号 | 任务 | 对应章节 | 改动面 |
|------|------|---------|--------|
| 2.1 | P2P 配置类从 `config/settings.py` 迁到 `modules/p2p/settings.py` | 第 5 章 | settings.py + 配置消费方 |
| 2.2 | `P2PRepository` 从 `core/database/repository.py` 迁到 `modules/p2p/repository.py` | 第 11 章 | core/database + api/main.py + tools.py |
| 2.3 | `modules/p2p/tools.py` 拆分为 `modules/p2p/tools/` 包 | 第 10 章 | 内部重组 |
| 2.4 | Prompt 分文件：ReportAgent prompt → prompts.py，早退模板 → prompts.py | 第 9 章 | report_agent.py + orchestrator.py |
| 2.5 | `__init__.py` 导出规范化 | 第 13 章 | core 各子包 __init__.py |

**测试验证**：
- 2.1 完成后：运行配置相关测试，验证 `config.yaml` 加载兼容
- 2.2 完成后：运行 `test_rules.py` + `test_api_routes.py`，验证 Repository 注入正常
- 2.3 完成后：运行工具相关测试，验证 19 个 tool 全部可调用
- Phase 结束时：`grep -r "from modules.p2p" config/settings.py core/database/repository.py` 无结果
- Phase 结束时：全量 `pytest`，覆盖率 ≥ 90%

**产出**：core 层不含 P2P 业务代码，config/settings.py 仅保留通用配置。

#### Phase 3：Orchestrator 瘦身（提取非编排逻辑）

从 orchestrator.py 中提取不属于编排职责的逻辑，为 Phase 4 统一 DAG 做准备。本阶段不改编排流程。

| 序号 | 任务 | 对应章节 | 改动面 |
|------|------|---------|--------|
| 3.1 | 实体处理提取到 `core/orchestrator/entity.py`（指代消解 + 验证 + 补充，315 行） | 第 4 章 4.3.3 | orchestrator.py → entity.py |
| 3.2 | 短期记忆提取到 `core/memory/short_term.py`（checkpointer 管理 + 读写，200 行） | 第 4 章 4.3.4 | orchestrator.py → short_term.py |
| 3.3 | output_mode prompt 提取到 `core/orchestrator/prompts.py` | 第 9 章 | orchestrator.py → prompts.py |

**测试验证**：
- 3.1 完成后：运行指代消解 + 实体验证相关测试
- 3.1 完成后：验证新增 span `entity / resolve_references` 在 trace 中出现
- 3.2 完成后：运行短期记忆读写测试
- 3.3 完成后：验证新增 span `orchestrator / resolve_output_mode` 在 trace 中出现
- Phase 结束时：orchestrator.py 行数从 1544 降至约 700 行
- Phase 结束时：全量 `pytest`，覆盖率 ≥ 90%

**产出**：orchestrator.py 仅保留编排流程控制（路由决策、执行分发、结果封装）。

#### Phase 4：ModuleProvider + 统一 DAG 执行模型

引入 ModuleProvider Protocol，Orchestrator 不再直接 import P2P 模块；DAG 和 ReAct 合并为统一执行入口。

| 序号 | 任务 | 对应章节 | 改动面 |
|------|------|---------|--------|
| 4.1 | 定义 `ModuleProvider` Protocol（`core/orchestrator/provider.py`） | 第 4 章 4.3.2 | 新增文件 |
| 4.2 | 实现 `P2PModuleProvider`（`modules/p2p/provider.py`） | 第 4 章 4.3.2 | 新增文件 |
| 4.3 | Orchestrator 改用 ModuleProvider 获取 agent/repository/entity 规则 | 第 4 章 4.3.5 | orchestrator.py 重构 |
| 4.4 | DAGExecutor 新增 `agent` 任务类型 | 第 6 章 6.3.1 | executor.py 新增分支 |
| 4.5 | Orchestrator 删除 `if use_dag` 分支，统一走 DAG 入口 | 第 4 章 4.3.1 | 删除 `_execute_react()`，记忆写入统一 |
| 4.6 | `api/main.py` 改为装配 ModuleProvider → Orchestrator | 第 4 章 4.3.5 | main.py lifespan |

**测试验证**：
- 4.1-4.2 完成后：编写 `test_provider.py`，验证 P2PModuleProvider 接口完整性
- 4.3 完成后：验证 `grep -r "from modules.p2p" core/orchestrator/` 无结果
- 4.4 完成后：编写 agent 任务类型单元测试
- 4.5 完成后：运行 DAG 路径 + ReAct 路径端到端测试，验证行为不变
- 4.5 完成后：验证 agent 任务类型的 span `dag.task / {task_id}:agent` 在 trace 中出现
- Phase 结束时：全量 `pytest`，覆盖率 ≥ 90%
- Phase 结束时：全链路 trace 验证（14.2 span 覆盖表全部通过）

**产出**：Orchestrator 不 import 任何 P2P 代码，所有执行统一走 DAG。

#### Phase 5：DAG 子系统配置化

DAG registry/templates/validator 从硬编码改为 ModuleProvider 驱动。

| 序号 | 任务 | 对应章节 | 改动面 |
|------|------|---------|--------|
| 5.1 | ToolRegistry 从 `ModuleProvider.get_tools()` 自动注册 | 第 6 章 6.3.2 | registry.py 重构 |
| 5.2 | DAG 模板迁到 `modules/p2p/dag_templates.py`，templates.py 改为聚合入口 | 第 6 章 6.3.3 | templates.py + 新增文件 |
| 5.3 | DAGValidator 从 ToolRegistry 元数据派生工具分类 | 第 6 章 6.3.4 | validator.py 重构 |

**测试验证**：
- 5.1 完成后：验证 `grep -r "from modules.p2p.tools" core/orchestrator/dag/registry.py` 无结果
- 5.2 完成后：运行 DAG 模板加载测试，验证所有 11 个模板正常加载
- 5.3 完成后：运行 DAG 校验测试
- Phase 结束时：全量 `pytest`，覆盖率 ≥ 90%

**产出**：DAG 子系统完全由 ModuleProvider 驱动，无硬编码工具列表和模板。

#### Phase 6：IntentRouter 拆分 + KnowledgeGraph 泛化

路由层和知识层的可扩展性重构。

| 序号 | 任务 | 对应章节 | 改动面 |
|------|------|---------|--------|
| 6.1 | `router.py` 拆分为 `router/` 包（`__init__.py` + `l1_keyword.py` + `l2_semantic.py` + `l3_llm.py` + `prompts.py`） | 第 7 章 | router.py 1114 行 → 5 个文件 |
| 6.2 | L1 规则库迁到 `modules/p2p/intent_rules.py`，router 从 ModuleProvider 获取 | 第 7 章 | router + provider |
| 6.3 | L3 Prompt 模板化，从注册的 analysis_type 动态生成 | 第 7 章 | router/prompts.py |
| 6.4 | IntentRouter 阈值参数迁入 `config.yaml` | 第 7 章 | router + config.yaml |
| 6.5 | KnowledgeGraph 泛化为通用 CRUD，P2P 节点方法迁到 `modules/p2p/graph_schema.py` | 第 8 章 | graph.py + 新增文件 |

**测试验证**：
- 6.1 完成后：运行意图路由全量测试（`test_router.py`），验证 L1/L2/L3 行为不变
- 6.2 完成后：验证 `grep -r "_RULE_LIBRARY" core/orchestrator/` 无结果
- 6.3 完成后：验证 L3 prompt 中的 analysis_type 列表与注册类型一致
- 6.5 完成后：运行 graph 相关测试（Neo4j 默认禁用，验证接口兼容）
- Phase 结束时：全量 `pytest`，覆盖率 ≥ 90%

**产出**：路由层按策略拆分，规则可配置；知识层可扩展。

#### Phase 7：测试结构 + 全链路验收

最后阶段：测试结构调整 + 全项目验收。

| 序号 | 任务 | 对应章节 | 改动面 |
|------|------|---------|--------|
| 7.1 | conftest.py 按层级拆分（P2P fixture → tests/fixtures/p2p.py） | 第 12 章 | tests/conftest.py + 子目录 |
| 7.2 | `config.yaml` 开关分组注释 | 第 14.3 节 | config.yaml |
| 7.3 | 全链路 trace span 验收 | 第 14.2 节 | DAG + ReAct 各一次请求 |
| 7.4 | 全量测试 + 覆盖率验收 | — | 全部测试通过，覆盖率 ≥ 90% |

**测试验证**：
- 7.1 完成后：验证根 conftest.py 无 `mock_po_data` 等 P2P fixture
- 7.3 执行：通过 `GET /traces` 查询，逐项核对 14.2 span 覆盖表全部通过
- 7.4 执行：`pytest --cov=. --cov-report=term-missing --cov-fail-under=90`

**产出**：重构完成，全部验收通过。

### 14.5 当前目录结构（重构前）

标注说明：`[!]` = 有问题待重构，`[ok]` = 无需改动

```
eragent/
├── alembic.ini
├── pyproject.toml
├── api/
│   ├── __init__.py
│   ├── main.py                              [!] 硬编码 set_repository(P2PRepository)
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── analyze.py                       [ok]
│   │   ├── analyze_async.py                 [ok]
│   │   ├── sessions.py                      [ok]
│   │   └── traces.py                        [ok]
│   └── schemas/
│       ├── __init__.py
│       ├── analysis.py                      [!] 业务领域模型与 HTTP 接口模型混杂，被 core/modules 11 处 import
│       ├── session.py                       [ok]
│       └── trace.py                         [ok]
├── config/
│   ├── __init__.py
│   ├── config.yaml                          [ok]
│   ├── intent_seeds.yaml                    [ok]
│   ├── crypto.py                            [ok]
│   └── settings.py                          [!] 混入 7 个 P2P 专属配置类
├── core/
│   ├── __init__.py
│   ├── logging_utils.py                     [ok]
│   ├── time_utils.py                        [ok]
│   ├── chat/
│   │   ├── __init__.py
│   │   ├── repository.py                    [ok]
│   │   └── tables.py                        [ok]
│   ├── database/
│   │   ├── __init__.py                      [!] 直接导出 P2PRepository
│   │   ├── engine.py                        [ok]
│   │   ├── init_db.py                       [ok]
│   │   ├── models.py                        [ok]
│   │   └── repository.py                    [!] 包含 P2P 专属查询方法
│   ├── knowledge/
│   │   ├── __init__.py
│   │   ├── embeddings.py                    [ok] 已是 Protocol 模式
│   │   ├── graph.py                         [!] 硬编码 5 个 P2P 节点方法 + id_field_mapping
│   │   └── vector_store.py                  [ok]
│   ├── memory/
│   │   ├── __init__.py
│   │   ├── long_term.py                     [ok]
│   │   ├── middleware.py                    [!] 命名不表达职责，实为 ToolMessage 输入裁剪
│   │   └── tables.py                        [ok]
│   │   # 注意：短期记忆逻辑散落在 orchestrator.py 中（约 200 行），此目录下无对应文件
│   ├── observability/
│   │   ├── __init__.py
│   │   ├── checkpointer.py                  [ok]
│   │   ├── console.py                       [ok]
│   │   ├── display_labels.py                [ok]
│   │   ├── middleware.py                     [!] 1260行，trace/span 管理与 SSE 流式事件推送混杂
│   │   ├── store.py                         [ok]
│   │   └── tables.py                        [ok]
│   ├── ontology/
│   │   ├── __init__.py
│   │   ├── loader.py                        [ok]
│   │   └── reasoner.py                      [ok]
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   ├── intent.py                        [ok] 旧兼容入口
│   │   ├── orchestrator.py                  [!] 1544行，硬编码 P2P import×6，实体处理315行内嵌，短期记忆200行内嵌，DAG/ReAct 双分支，output_mode prompt 内嵌
│   │   ├── router.py                        [!] 1114行，L1规则/L3 prompt/角色描述硬编码，职责×6
│   │   ├── signal.py                        [ok]
│   │   └── dag/
│   │       ├── __init__.py
│   │       ├── case_store.py                [ok]
│   │       ├── executor.py                  [!] _REPORT_TOOLS 硬编码
│   │       ├── registry.py                  [!] 硬编码 import 19 个 P2P 工具
│   │       ├── tables.py                    [ok]
│   │       ├── templates.py                 [!] 791行，11个模板+映射全部硬编码 P2P
│   │       └── validator.py                 [!] 硬编码 3 组工具分类
│   └── tasks/
│       ├── __init__.py
│       ├── context.py                       [ok]
│       ├── events.py                        [ok]
│       ├── events_redis.py                  [ok]
│       ├── registry.py                      [!] 反向 import api.schemas
│       ├── schemas.py                       [!] 反向 import api.schemas
│       └── stream_utils.py                  [ok]
├── modules/
│   ├── __init__.py
│   └── p2p/
│       ├── __init__.py
│       ├── agent.py                         [!] 反向 import api.schemas
│       ├── errors.py                        [ok]
│       ├── model_factory.py                 [!] 通用 LLM 工厂放在 P2P 下，被 core 层调用
│       ├── prompts.py                       [ok] 但缺少 ReportAgent prompt
│       ├── report_agent.py                  [!] prompt 内嵌在文件中
│       ├── tools.py                         [!] 1058行，19个工具单文件
│       ├── mock_data/
│       │   ├── __init__.py
│       │   └── generator.py                 [ok]
│       ├── ontology/
│       │   └── p2p.owl                      [ok]
│       └── rules/
│           ├── __init__.py                  [ok]
│           ├── _utils.py                    [ok]
│           ├── payment_compliance.py        [!] 反向 import api.schemas
│           ├── price_variance.py            [!] 反向 import api.schemas
│           ├── supplier_performance.py      [!] 反向 import api.schemas
│           └── three_way_match.py           [!] 反向 import api.schemas
├── migrations/
│   ├── env.py                               [ok]
│   ├── script.py.mako                       [ok]
│   └── versions/                            [ok] 9 个迁移文件
└── scripts/
    └── deploy_timezone.py                   [ok]
```

### 14.6 重构后目录结构

标注说明：`[新增]` `[迁入]` `[提取]` `[拆分]` `[改造]` `[删除]` `[不变]`

```
eragent/
├── alembic.ini                                                  [不变]
├── pyproject.toml                                               [不变]
├── api/
│   ├── __init__.py                                              [不变]
│   ├── main.py                                                  [改造] 装配 ModuleProvider → Orchestrator
│   ├── routes/
│   │   ├── __init__.py                                          [不变]
│   │   ├── analyze.py                                           [不变]
│   │   ├── analyze_async.py                                     [不变]
│   │   ├── sessions.py                                          [不变]
│   │   └── traces.py                                            [不变]
│   └── schemas/
│       ├── __init__.py                                          [不变]
│       ├── domain.py                                            [新增] 业务领域模型（从 analysis.py 拆出）
│       ├── analysis.py                                          [改造] 仅保留 HTTP 接口模型 AnalysisRequest
│       ├── session.py                                           [不变]
│       └── trace.py                                             [不变]
├── config/
│   ├── __init__.py                                              [不变]
│   ├── config.yaml                                              [改造] 新增 intent_router 参数段
│   ├── intent_seeds.yaml                                        [不变]
│   ├── crypto.py                                                [不变]
│   └── settings.py                                              [改造] 去掉 7 个 P2P 配置类，仅保留通用配置
├── core/
│   ├── __init__.py                                              [不变]
│   ├── logging_utils.py                                         [不变]
│   ├── time_utils.py                                            [不变]
│   ├── llm/
│   │   ├── __init__.py                                          [新增]
│   │   └── model_factory.py                                     [迁入] 从 modules/p2p/model_factory.py 迁入
│   ├── chat/
│   │   ├── __init__.py                                          [不变]
│   │   ├── repository.py                                        [不变]
│   │   └── tables.py                                            [不变]
│   ├── database/
│   │   ├── __init__.py                                          [改造] 去掉 P2PRepository 导出
│   │   ├── engine.py                                            [不变]
│   │   ├── init_db.py                                           [不变]
│   │   ├── models.py                                            [不变]
│   │   └── repository.py                                        [改造] 仅保留通用基类
│   ├── knowledge/
│   │   ├── __init__.py                                          [不变]
│   │   ├── embeddings.py                                        [不变]
│   │   ├── graph.py                                             [改造] 泛化为通用 CRUD，去掉 P2P 节点方法
│   │   └── vector_store.py                                      [不变]
│   ├── memory/
│   │   ├── __init__.py                                          [改造] 新增 ShortTermMemory 导出
│   │   ├── long_term.py                                         [不变]
│   │   ├── short_term.py                                        [新增] 从 orchestrator.py 提取 checkpointer 管理+读写
│   │   ├── trimmer.py                                           [改名] middleware.py → trimmer.py（ToolMessage 输入裁剪）
│   │   └── tables.py                                            [不变]
│   ├── observability/
│   │   ├── __init__.py                                          [不变]
│   │   ├── checkpointer.py                                      [不变]
│   │   ├── console.py                                           [不变]
│   │   ├── display_labels.py                                    [不变]
│   │   ├── tracing.py                                           [拆分+改名] middleware.py 拆出 tracing 部分（~900行）
│   │   ├── streaming.py                                         [拆分] middleware.py 拆出 SSE 事件推送（~120行）
│   │   ├── store.py                                             [不变]
│   │   └── tables.py                                            [不变]
│   ├── ontology/
│   │   ├── __init__.py                                          [不变]
│   │   ├── loader.py                                            [不变]
│   │   └── reasoner.py                                          [不变]
│   ├── orchestrator/
│   │   ├── __init__.py                                          [不变]
│   │   ├── intent.py                                            [不变]
│   │   ├── orchestrator.py                                      [改造] 统一 DAG 入口，依赖 ModuleProvider
│   │   ├── provider.py                                          [新增] ModuleProvider Protocol
│   │   ├── entity.py                                            [提取] 从 orchestrator.py 提取实体处理（指代消解+验证+补充，315行）
│   │   ├── prompts.py                                           [提取] 从 orchestrator.py 提取 output_mode 指令
│   │   ├── signal.py                                            [不变]
│   │   ├── router/
│   │   │   ├── __init__.py                                      [新增] IntentRouter 入口（组合三层策略）
│   │   │   ├── prompts.py                                       [提取] 从 router.py 提取 L3 分类 prompt
│   │   │   ├── l1_keyword.py                                    [拆分] 从 router.py 拆出 L1 关键词匹配
│   │   │   ├── l2_semantic.py                                   [拆分] 从 router.py 拆出 L2 语义匹配
│   │   │   └── l3_llm.py                                        [拆分] 从 router.py 拆出 L3 LLM 分类
│   │   └── dag/
│   │       ├── __init__.py                                      [不变]
│   │       ├── case_store.py                                    [不变]
│   │       ├── executor.py                                      [改造] 新增 agent 任务类型
│   │       ├── registry.py                                      [改造] 从 ModuleProvider 自动注册
│   │       ├── tables.py                                        [不变]
│   │       ├── templates.py                                     [改造] 聚合各模块模板
│   │       └── validator.py                                     [改造] 从 registry 元数据派生工具分类
│   └── tasks/
│       ├── __init__.py                                          [不变]
│       ├── context.py                                           [不变]
│       ├── events.py                                            [不变]
│       ├── events_redis.py                                      [不变]
│       ├── registry.py                                          [改造] import 改为 api/schemas/domain
│       ├── schemas.py                                           [改造] import 改为 api/schemas/domain
│       └── stream_utils.py                                      [不变]
├── modules/
│   ├── __init__.py                                              [不变]
│   └── p2p/
│       ├── __init__.py                                          [不变]
│       ├── provider.py                                          [新增] P2PModuleProvider（实现 ModuleProvider）
│       ├── settings.py                                          [迁入] 从 config/settings.py 迁入 7 个 P2P 配置类
│       ├── repository.py                                        [迁入] 从 core/database/repository.py 迁入 P2P 查询
│       ├── graph_schema.py                                      [提取] 从 core/knowledge/graph.py 提取 P2P 节点方法
│       ├── dag_templates.py                                     [迁入] 从 core/orchestrator/dag/templates.py 迁入模板
│       ├── intent_rules.py                                      [提取] 从 router.py 提取 _RULE_LIBRARY
│       ├── agent.py                                             [改造] import 改为 api/schemas/domain + core/llm
│       ├── errors.py                                            [不变]
│       ├── prompts.py                                           [改造] 合入 ReportAgent prompt
│       ├── report_agent.py                                      [改造] prompt 提取到 prompts.py
│       ├── tools/
│       │   ├── __init__.py                                      [拆分] 统一导出 + set_repository()
│       │   ├── _output.py                                       [拆分] _clip_and_dump 输出裁剪
│       │   ├── _inject.py                                       [拆分] _repository 注入管理
│       │   ├── query.py                                         [拆分] 4 个数据查询工具
│       │   ├── analysis.py                                      [拆分] 5 个规则分析工具
│       │   ├── advanced.py                                      [拆分] 5 个高级分析工具
│       │   └── stub.py                                          [拆分] 5 个预留工具
│       ├── mock_data/
│       │   ├── __init__.py                                      [不变]
│       │   └── generator.py                                     [不变]
│       ├── ontology/
│       │   └── p2p.owl                                          [不变]
│       └── rules/
│           ├── __init__.py                                      [不变]
│           ├── _utils.py                                        [不变]
│           ├── payment_compliance.py                            [改造] import 改为 api/schemas/domain
│           ├── price_variance.py                                [改造] import 改为 api/schemas/domain
│           ├── supplier_performance.py                          [改造] import 改为 api/schemas/domain
│           └── three_way_match.py                               [改造] import 改为 api/schemas/domain
├── migrations/
│   ├── env.py                                                   [不变]
│   ├── script.py.mako                                           [不变]
│   └── versions/                                                [不变]
└── scripts/
    └── deploy_timezone.py                                       [不变]
```

**删除的文件：**
- `modules/p2p/model_factory.py` → 迁入 `core/llm/model_factory.py`
- `modules/p2p/tools.py` → 拆分为 `modules/p2p/tools/` 包
- `core/orchestrator/router.py` → 拆分为 `core/orchestrator/router/` 包
- `core/observability/middleware.py` → 拆分为 `tracing.py` + `streaming.py`
- `core/memory/middleware.py` → 改名为 `trimmer.py`

### 14.7 每阶段验收标准

| 阶段 | 验收条件 |
|------|---------|
| **每个 Phase** | 全量 `pytest` 通过，覆盖率 ≥ 90% |
| **Phase 1** | `grep -r "from core.observability.middleware" --include="*.py"` 无结果；`grep -r "from modules.p2p.model_factory" --include="*.py"` 无结果 |
| **Phase 2** | `config/settings.py` 无 P2P 配置类；`core/database/repository.py` 无 P2P 查询方法；`core/` 各子包 `__init__.py` 仅导出公共 API |
| **Phase 3** | `orchestrator.py` 行数 ≤ 800；`entity.py` / `short_term.py` / `prompts.py` 独立可测试；新增 span `resolve_references` / `resolve_output_mode` 在 trace 中出现 |
| **Phase 4** | `grep -r "from modules.p2p" core/orchestrator/orchestrator.py` 无结果；DAG 路径和 ReAct 路径端到端行为与重构前一致；agent 任务类型 span 在 trace 中出现 |
| **Phase 5** | `grep -r "from modules.p2p" core/orchestrator/dag/` 无结果；11 个 DAG 模板全部从 ModuleProvider 加载 |
| **Phase 6** | `router.py` 不存在（已拆为 router/ 包）；`_RULE_LIBRARY` 不在 core 层；L3 prompt 的 analysis_type 列表动态生成 |
| **Phase 7** | 根 conftest.py 无 P2P fixture；14.2 span 覆盖表全部通过；`config.yaml` 开关按 L1-L4 分组 |
