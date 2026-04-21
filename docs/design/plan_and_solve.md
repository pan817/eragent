# Plan and Solve 模式设计方案

> 状态：设计阶段
> 日期：2026-04-21
> 作者：Claude + 项目团队

---

## 目录

1. [背景与问题分析](#1-背景与问题分析)
2. [现有架构分析](#2-现有架构分析)
3. [Plan and Solve 模式概述](#3-plan-and-solve-模式概述)
4. [方案选型与决策过程](#4-方案选型与决策过程)
5. [详细设计](#5-详细设计)
6. [与现有架构的集成](#6-与现有架构的集成)
7. [失败处理与降级策略](#7-失败处理与降级策略)
8. [性能分析与预期收益](#8-性能分析与预期收益)
9. [实施计划](#9-实施计划)
10. [测试策略](#10-测试策略)
11. [学习扩展：动态 DAG 沉淀与复用](#11-学习扩展动态-dag-沉淀与复用)

---

## 1. 背景与问题分析

### 1.1 核心痛点

当前 Orchestrator 的路由决策中，**大部分用户查询最终走 ReAct 路径**（L3 兜底），而非 DAG 并行执行。这导致两个直接问题：

| 问题 | 影响 |
|------|------|
| **延迟高** | ReAct 每步都需要 LLM 推理，典型查询需要 2-4 轮 LLM 调用，串行执行 |
| **Token 消耗大** | 每轮 LLM 调用都要携带完整的 system prompt + 工具 schema + 对话历史 |

技术债 #1（`docs/agent_issue.md`）也明确记录了这个问题："当前 IntentRouter 把较多查询兜底到 ReAct 路径（L3 LLM 分类），DAG 命中率不够高。"

### 1.2 用户查询特征分析

走 ReAct 路径的高频查询具有以下共同特征：

**典型查询示例**：
- "查询最新的一个 PO"
- "查询最近的异常采购订单"
- "有没有逾期未付的发票"
- "哪些供应商交货延迟了"

**特征归纳**：

| 特征 | 说明 |
|------|------|
| 无具体实体 ID | 没有给出 PO-xxx / SUP-xxx 等编号，无法命中实体维度 DAG 模板 |
| 非明确分析类型 | 不是"做三路匹配"/"做价格差异分析"，无法命中分析类型 DAG 模板 |
| 探索性查询 | 用户先看数据全貌，再决定是否深入某个方向 |
| 步骤可预判 | 虽然没有现成模板，但拿到查询后即可确定需要调哪些工具 |

**关键洞察**：这类查询并非真正需要 ReAct 的"边走边看"能力——它们不需要根据中间结果动态调整策略。它们只是没有对应的静态 DAG 模板，被迫走了 ReAct 兜底。

### 1.3 问题本质

现有架构存在一个**覆盖空白**：

```
确定性高 ◄──────────────────────────────────────────► 确定性低
静态 DAG（15 个模板）     ???（空白地带）          ReAct（边想边做）
```

- 静态 DAG 只覆盖预定义的分析场景
- ReAct 覆盖所有场景但性能差
- 中间缺少一层：**"可以规划但没有现成模板"的查询**

Plan and Solve 模式正是填补这个空白的方案。

---

## 2. 现有架构分析

### 2.1 四条执行路径

当前 Orchestrator（`core/orchestrator/orchestrator.py`）的 `_analyze_inner` 方法实现了四条执行路径：

```
用户查询 → IntentRouter 三级路由（L0 bypass → L1 关键词 → L2 语义 → L3 LLM）
  │
  ├─ 早退路由（META / CHITCHAT / OUT_OF_SCOPE）
  │   → 模板响应，零 LLM 调用
  │
  ├─ Lookup 快捷路径（DATA_LOOKUP + 有实体/关键词）
  │   → 直调工具，零 LLM 调用
  │
  ├─ 静态 DAG（L1/L2 命中 + 有对应模板）
  │   → DAGExecutor 并行执行 → ReportAgent 汇总
  │
  └─ ReAct 兜底（L3 / 低置信度 / 无模板）
      → P2PAgent 串行 tool-calling 循环
```

### 2.2 各路径优劣势

| 路径 | LLM 调用次数 | 并行能力 | 覆盖范围 | 质量稳定性 |
|------|-------------|---------|---------|-----------|
| 早退路由 | 0 | N/A | 窄（闲聊/META） | 高（模板） |
| Lookup 快捷路径 | 0 | N/A | 窄（简单事实查询） | 高（直调工具） |
| 静态 DAG | 1（ReportAgent） | 支持并行 | 中（15 个模板） | 高（确定性） |
| ReAct | 2-4（每步推理） | 不支持 | 广（任意查询） | 低（LLM 可能遗漏/出错） |

### 2.3 静态 DAG 模板覆盖范围

当前共 15 个静态模板（`modules/p2p/dag_templates.py`）：

**分析类型维度（10 个）**：
- THREE_WAY_MATCH / PRICE_VARIANCE / PAYMENT_COMPLIANCE / SUPPLIER_PERFORMANCE
- SPEND_ANALYSIS / RECEIPT_ANOMALY / INVOICE_DUPLICATE
- DISCOUNT_UTILIZATION / PO_CYCLE_TIME / VENDOR_CONCENTRATION

**实体维度（4 个）**：
- payment_single / invoice_single / po_risk / supplier_risk

**通用概览（1 个）**：
- recent_procurement_health

**命中条件**：
- 分析类型模板：路由必须识别出具体的 AnalysisType（非 COMPREHENSIVE）
- 实体维度模板：必须有具体实体 ID + COMPREHENSIVE 意图
- 通用概览模板：必须包含采购词 + 概览意图词

这意味着：**"查询最新的 PO"这种不含实体 ID、不属于明确分析类型的查询，无法命中任何模板**。

### 2.4 ReAct 执行流程详解

ReAct 路径的完整执行链路（`modules/p2p/agent.py`）：

```
Orchestrator._execute_dag(use_agent_fallback=True)
  → DAGExecutor 收到 type="agent" 的单节点任务
    → P2PAgent.run() / analyze()
      → 构建 invoke_messages（system prompt + 长期记忆 + 用户查询）
      → agent.ainvoke()（LangChain ReAct 循环）
        → LLM 推理 #1：选择工具 + 生成参数
        → 工具执行：query_purchase_orders(...)
        → LLM 推理 #2：处理工具结果，决定下一步
        → 工具执行：run_three_way_match(...)
        → LLM 推理 #3：生成最终回复
      → 解析响应（JSON / Markdown）
      → 写入长期记忆
```

**关键性能瓶颈**：每一步 LLM 推理都携带完整的上下文（system prompt ~2000 tokens + 工具 schema ~3000 tokens + 对话历史），且必须串行等待。

### 2.5 现有可复用基础设施

| 组件 | 位置 | Plan and Solve 可复用性 |
|------|------|----------------------|
| DAGExecutor | `core/orchestrator/dag/executor.py` | **直接复用**：接受 task 列表，拓扑排序并行执行 |
| ToolRegistry | `core/orchestrator/dag/registry.py` | **直接复用**：tool_name → callable 查找 |
| ReportAgent | `modules/p2p/report_agent.py` | **直接复用**：汇总工具输出生成报告 |
| build_chat_model | `core/llm/model_factory.py` | **直接复用**：支持 main/fast 模型切换 |
| QuerySignal | `core/orchestrator/signal.py` | **直接复用**：路由结果数据结构 |
| IntentRoutingSettings | `config/settings.py` | **扩展**：新增 plan_and_solve 配置字段 |

---

## 3. Plan and Solve 模式概述

### 3.1 定义

Plan and Solve（计划与求解）是一种两阶段 LLM 推理模式：

1. **Planning 阶段**：LLM 接收用户查询和可用工具清单，输出一个结构化的执行计划（步骤列表，包括工具名、参数、步骤间依赖关系）
2. **Solving 阶段**：按计划逐步执行工具调用，无依赖的步骤可以并行

该模式源自论文 *"Plan-and-Solve Prompting"*（Wang et al., 2023），核心思想是将复杂问题的求解分为"制定计划"和"执行计划"两个显式阶段，而非让 LLM 在单一循环中同时承担规划和执行。

### 3.2 与 ReAct 的本质区别

| 维度 | ReAct | Plan and Solve |
|------|-------|----------------|
| 决策时机 | 每一步实时决策 | 执行前一次性规划 |
| 执行方式 | 串行（observe → think → act 循环） | 可并行（无依赖步骤同时执行） |
| LLM 调用次数 | N 次（N = 工具调用数 + 1） | 1 次 planning + 1 次 report = 2 次 |
| 适应性 | 强（每步可根据中间结果调整） | 弱（计划生成后不易更改） |
| 确定性 | 低（相同查询可能走不同路径） | 高（相同查询生成相同计划） |
| Token 消耗 | 高（每轮携带完整上下文） | 低（只有 planning 和 report 两轮） |

### 3.3 适用场景判断

**适合 Plan and Solve 的查询**（步骤可预判）：
- "查询最新的 PO" → 调 `query_purchase_orders(days=7)` → 格式化
- "最近有没有异常采购订单" → 并行调 `query_purchase_orders` + `run_three_way_match` → 汇总
- "对比供应商 A 和 B 的绩效" → 并行调两次 `calculate_supplier_kpis` → 对比

**不适合 Plan and Solve 的查询**（需要动态决策）：
- "帮我查一下 PO-1001 有什么问题" → 先查 PO → 发现匹配异常 → **根据异常类型决定**追查收货还是发票
- "为什么这个月付款金额突增" → 先查汇总 → 发现某供应商异常 → **动态决定**深入方向

**判断标准**：查询的下一步操作是否依赖上一步的具体返回值。如果是，走 ReAct；如果不是，走 Plan and Solve。

---

## 4. 方案选型与决策过程

### 4.1 三个候选方案

**方案 A：用 Plan and Solve 替代 ReAct（L3 路径增强）**

```
L3 兜底 → Plan and Solve（完全替代 ReAct）
```

- 优点：架构简单，只有一条兜底路径
- 缺点：无法处理需要动态决策的查询，回退能力丧失

**方案 B：用 Plan and Solve 替代静态 DAG（动态模板生成）**

```
所有分析查询 → LLM 动态生成 DAG → DAGExecutor 执行
```

- 优点：彻底解决模板覆盖率问题
- 缺点：已有 15 个成熟模板的场景反而变慢（多一次 LLM 调用），可靠性不如静态模板

**方案 C：混合模式（静态 DAG 不变 + Plan and Solve 替代 ReAct 主路径 + ReAct 保留兜底）**

```
L1/L2 命中 → 静态 DAG（不变）
L3 兜底 → Planning LLM 判断
            ├─ 能提前规划 → Plan and Solve（并行执行）
            └─ 需要动态决策 → ReAct（保留）
```

### 4.2 选择方案 C 的理由

| 考量 | 结论 |
|------|------|
| 静态 DAG 是否保留 | **保留**。15 个模板在其覆盖场景内是最优解，确定性高、性能好 |
| ReAct 是否保留 | **保留**。真正需要动态推理的查询仍然需要 ReAct |
| Plan and Solve 的定位 | **填补空白**。接管"可以规划但没有模板"的查询，即当前走 ReAct 的大部分流量 |

### 4.3 关键设计决策记录

以下决策在方案讨论阶段逐一确认：

| 决策点 | 结论 | 理由 |
|--------|------|------|
| Plan and Solve 与 ReAct 的关系 | 共存 | Plan and Solve 处理可预判的查询，ReAct 处理需动态决策的查询 |
| Planning 模型选择 | 可配置（fast / main） | `config.yaml` 中配置，默认用 fast 模型（性能优先）|
| 执行引擎 | 复用 DAGExecutor | 避免重复建设，Planning 输出直接映射为 DAG task 列表 |
| 优先级 | 性能 > 质量 | 减少 LLM 调用次数和延迟是首要目标 |

### 4.4 合理性与可行性评估

**合理性**：
- 解决的是真实高频痛点（大部分查询走 ReAct 导致慢且贵）
- 不推倒重来，填补静态 DAG 和 ReAct 之间的空白
- 性能收益可预期（LLM 调用次数从 2-4 次降到 2 次）

**可行性**：
- DAGExecutor 可直接复用（已支持拓扑排序 + 并行执行 + 超时控制 + trace）
- LLM 结构化输出可靠（qwen3-max tool-calling 准确率已验证）
- 工具集封闭可枚举（25 个工具有明确的名称和参数定义）
- 架构预留了扩展点（`_analyze_inner` 的 `use_dag` 分支可自然扩展）

**风险**：
- Planning 失败 → 降级到 ReAct 兜底，不比现状差
- Planning Prompt 需要迭代调优 → 非架构风险，可渐进改进

---

## 5. 详细设计

### 5.1 新增模块：`core/orchestrator/planner.py`

Planner 类的设计参考 `modules/p2p/report_agent.py` 的模式：延迟初始化 LLM、可配置模型选择、结构化输出。

#### 5.1.1 类接口定义

```python
class ExecutionPlan:
    """LLM 生成的执行计划。"""
    plannable: bool           # True=可提前规划，False=需要动态决策（降级到 ReAct）
    reasoning: str            # LLM 的规划理由（用于 trace 和调试）
    tasks: list[dict]         # DAGExecutor 可执行的 task 列表（格式同静态 DAG）
    report_scenario: str      # ReportAgent 的 scenario 描述


class Planner:
    """Plan and Solve 规划器。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._llm = None  # 延迟初始化

    def _ensure_llm(self) -> Any:
        """延迟构建 LLM（参考 ReportAgent._ensure_llm）。"""
        if self._llm is not None:
            return self._llm
        from core.llm.model_factory import build_chat_model
        cfg = self._settings.plan_and_solve
        llm_cfg = self._settings.llm_fast if cfg.use_fast_model else self._settings.llm
        self._llm = build_chat_model(
            llm_cfg,
            disable_thinking=True,
            max_tokens_override=cfg.max_planning_tokens,
        )
        return self._llm

    async def plan(
        self,
        query: str,
        signal: QuerySignal,
        available_tools: list[ToolInfo],
        params: dict[str, Any],
    ) -> ExecutionPlan:
        """为用户查询生成执行计划。

        Args:
            query: 增强后的用户查询（已完成指代消解）。
            signal: 路由结果（含 intent_kind、entities 等）。
            available_tools: 当前模式下可用的工具信息列表。
            params: 已解析的参数（days、vendor_id 等）。

        Returns:
            ExecutionPlan：plannable=True 时包含可执行的 task 列表；
            plannable=False 时表示需降级到 ReAct。
        """
        ...

    def validate_plan(self, plan: ExecutionPlan, registry: ToolRegistry) -> list[str]:
        """校验计划合法性，返回错误列表（空列表表示通过）。

        校验内容：
        - 每个 task 的 tool_name 在 ToolRegistry 中存在
        - depends_on 引用的 task_id 存在且无循环依赖
        - 必填参数不为空
        """
        ...
```

#### 5.1.2 ExecutionPlan 数据结构

ExecutionPlan 的 `tasks` 字段直接复用现有 DAG task dict 格式，确保与 DAGExecutor 零适配：

```python
# LLM 输出的计划（JSON）→ 直接作为 DAGExecutor.execute(tasks) 的输入
{
    "plannable": True,
    "reasoning": "用户要查询最新的 PO，只需调用 query_purchase_orders 即可",
    "tasks": [
        {
            "task_id": "t1",
            "tool_name": "query_purchase_orders",
            "inputs": {"days": 7, "vendor_id": "", "po_number": ""},
            "depends_on": [],
            "timeout_sec": 60,
            "output_key": "po_data"
        },
        {
            "task_id": "t_report",
            "tool_name": "generate_summary_report",
            "inputs": {"scenario": "最新采购订单查询"},
            "depends_on": ["t1"],
            "timeout_sec": 120,
            "output_key": "report"
        }
    ],
    "report_scenario": "最新采购订单查询"
}
```

**与静态 DAG 模板的格式对比**：

| 字段 | 静态 DAG | Plan and Solve |
|------|----------|----------------|
| task_id | t1, t2, ... | 相同 |
| tool_name | 工具名 | 相同 |
| inputs | 含 `{days}` 占位符 | 已替换为实际值 |
| depends_on | 任务 ID 列表 | 相同 |
| timeout_sec | 固定值 | LLM 可不输出，用默认值 60 |
| output_key | 固定值 | 相同 |

唯一区别：静态 DAG 的 inputs 含占位符需要 `_replace_params` 替换，Plan and Solve 的 inputs 由 LLM 直接填入实际值。

### 5.2 Planning Prompt 设计

#### 5.2.1 Prompt 结构

```
系统角色 + 任务说明
  ↓
可用工具清单（名称 + 参数 + 一句话说明 + 分类）
  ↓
用户查询 + 已解析参数
  ↓
输出格式约束（JSON schema）
  ↓
判断规则（何时 plannable=true，何时 plannable=false）
```

#### 5.2.2 Prompt 模板

```python
_PLANNING_PROMPT = """你是一个 ERP 采购分析系统的执行规划器。
你的任务是：根据用户查询和可用工具，生成一个结构化的执行计划。

## 可用工具

### 精确查询（已知条件查具体记录）
{query_tools}

### 规则检测（检查合规性）
{analysis_tools}

### 聚合统计（汇总分析）
{advanced_tools}

{graph_tools_section}

## 用户查询
{query}

## 已解析参数
{params_json}

## 输出要求

请输出 JSON 格式的执行计划：

```json
{{
  "plannable": true/false,
  "reasoning": "一句话说明规划理由",
  "tasks": [
    {{
      "task_id": "t1",
      "tool_name": "工具名",
      "inputs": {{"参数名": "参数值"}},
      "depends_on": [],
      "output_key": "输出键名"
    }}
  ],
  "report_scenario": "报告场景描述"
}}
```

## 规划规则

1. **plannable=true 的条件**：拿到查询后能确定需要调哪些工具、传什么参数、步骤间什么依赖关系
2. **plannable=false 的条件**：下一步操作取决于上一步的具体返回值（例如"查 PO 发现异常后根据异常类型追查"）
3. **并行优化**：没有数据依赖的步骤 depends_on 设为空数组，执行器会自动并行
4. **最后一步必须是 generate_summary_report**：depends_on 指向所有前置数据步骤
5. **工具名必须从上述可用工具中选取**，不要编造不存在的工具
6. **inputs 中的参数值使用"已解析参数"中的实际值**，不要使用占位符
7. 如无法判断用户意图或工具无法满足需求，设 plannable=false

只输出 JSON，不要输出其他内容。"""
```

#### 5.2.3 工具信息格式化

从 `provider.get_tools()` 返回的 LangChain tool 对象中提取信息：

```python
@dataclass
class ToolInfo:
    """工具信息摘要（注入 Planning Prompt）。"""
    name: str          # tool.name
    description: str   # tool.description（docstring 第一行）
    parameters: dict   # tool.args_schema（JSON schema）


def extract_tool_info(tools: list) -> list[ToolInfo]:
    """从 LangChain tool 列表提取 Planning Prompt 所需的工具信息。"""
    result = []
    for tool in tools:
        desc_lines = (tool.description or "").strip().split("\n")
        result.append(ToolInfo(
            name=tool.name,
            description=desc_lines[0] if desc_lines else "",
            parameters=tool.args if hasattr(tool, "args") else {},
        ))
    return result
```

工具按 ToolRegistry 的分类（data / analysis）分组，格式与 `modules/p2p/prompts.py` 中的工具分类速查表对齐。

### 5.3 配置设计

#### 5.3.1 新增 PlanAndSolveSettings

在 `config/settings.py` 中新增配置类：

```python
class PlanAndSolveSettings(BaseSettings):
    """Plan and Solve 规划器配置。"""

    enabled: bool = Field(
        default=True,
        description="Plan and Solve 总开关：关闭时 L3 兜底一律走 ReAct（保持现有行为）",
    )
    use_fast_model: bool = Field(
        default=True,
        description="True 用 llm_fast，False 用 llm 主模型",
    )
    max_planning_tokens: int = Field(
        default=2000,
        description="Planning LLM 最大输出 token 数",
    )
    planning_timeout_sec: int = Field(
        default=30,
        description="Planning 阶段超时（秒），超时则降级到 ReAct",
    )
    validate_plan: bool = Field(
        default=True,
        description="是否校验 LLM 生成的计划（工具名、依赖关系合法性）",
    )

    model_config = {"env_prefix": "PLAN_SOLVE_"}
```

#### 5.3.2 config.yaml 新增段

```yaml
# Plan and Solve 规划器
plan_and_solve:
  enabled: true              # 总开关
  use_fast_model: true       # true=llm_fast, false=llm 主模型
  max_planning_tokens: 2000  # Planning 输出 token 上限
  planning_timeout_sec: 30   # Planning 超时（秒）
  validate_plan: true        # 校验 LLM 生成的计划合法性
```

#### 5.3.3 Settings 类注册

```python
# config/settings.py 的 Settings 类中新增字段
class Settings(BaseSettings):
    ...
    plan_and_solve: PlanAndSolveSettings = Field(default_factory=PlanAndSolveSettings)
```

---

## 6. 与现有架构的集成

### 6.1 Orchestrator 改动点

改动集中在 `core/orchestrator/orchestrator.py` 的 `_analyze_inner` 方法，**第 716 行**附近的路由决策段。

#### 6.1.1 当前逻辑（第 716-741 行）

```python
# 当前：二分支决策
if is_recall or is_data_lookup or low_confidence:
    use_dag = False    # → 走 ReAct
else:
    ...
    use_dag = (...)    # → 走 DAG 或 ReAct
```

#### 6.1.2 改造后逻辑

```python
# 改造后：三分支决策
if is_recall:
    use_dag = False
    use_plan_and_solve = False     # RECALL 必须走 ReAct（需要看短期记忆）
elif use_dag:
    use_plan_and_solve = False     # 有静态 DAG 模板，不需要动态规划
else:
    # L3 兜底：原来一律走 ReAct，现在先尝试 Plan and Solve
    if self._settings.plan_and_solve.enabled:
        use_plan_and_solve = True  # 由 Planner 决定是否可规划
    else:
        use_plan_and_solve = False # 开关关闭，保持原有 ReAct 行为
```

#### 6.1.3 执行分支

在第 800 行 `_execute_dag` 调用之前，插入 Plan and Solve 分支：

```python
if use_plan_and_solve:
    plan_result = await self._try_plan_and_solve(
        query=enhanced_query,
        signal=signal,
        params=parsed_params,
        report_id=report_id,
        trace_id=trace_id,
        ...
    )
    if plan_result is not None:
        result = plan_result  # Plan and Solve 成功
    else:
        # Plan and Solve 失败（plannable=false 或校验不通过），降级到 ReAct
        result = await self._execute_dag(
            ...,
            use_agent_fallback=True,  # 走 ReAct
        )
else:
    result = await self._execute_dag(
        ...,
        use_agent_fallback=not use_dag,
    )
```

#### 6.1.4 新增 `_try_plan_and_solve` 方法

```python
async def _try_plan_and_solve(self, ...) -> AnalysisResult | None:
    """尝试 Plan and Solve 路径。

    成功返回 AnalysisResult，失败返回 None（由调用方降级到 ReAct）。
    """
    planner = self._lazy_planner  # 延迟初始化

    # 1. 生成计划
    plan = await asyncio.wait_for(
        planner.plan(query, signal, available_tools, params),
        timeout=self._settings.plan_and_solve.planning_timeout_sec,
    )

    # 2. 判断是否可规划
    if not plan.plannable:
        _logger.info("planner decided not plannable: %s", plan.reasoning)
        return None

    # 3. 校验计划合法性
    if self._settings.plan_and_solve.validate_plan:
        errors = planner.validate_plan(plan, self._lazy_tool_registry)
        if errors:
            _logger.warning("plan validation failed: %s", errors)
            return None

    # 4. 复用 DAGExecutor 执行
    dag_result = await self._lazy_dag_executor.execute(
        tasks=plan.tasks,
        output_mode_prompt=output_mode_prompt,
        query=query,
        analysis_type=analysis_type.value,
    )

    # 5. 封装结果
    return AnalysisResult(...)
```

### 6.2 Planner 延迟初始化

在 Orchestrator 中新增 `_lazy_planner` 属性，与 `_lazy_agent`、`_lazy_dag_executor` 模式一致：

```python
@property
def _lazy_planner(self) -> Any:
    """延迟初始化 Planner（Plan and Solve 路径）。"""
    if self._planner is None:
        from core.orchestrator.planner import Planner
        self._planner = Planner(settings=self._settings)
    return self._planner
```

### 6.3 工具清单注入

Planning Prompt 需要知道当前可用的工具。通过 `P2PModuleProvider.get_tools()` 获取工具列表，提取 `ToolInfo` 后按分类格式化：

```python
# 在 _try_plan_and_solve 中
tools = self._provider.get_tools()
tool_infos = extract_tool_info(tools)
plan = await planner.plan(query, signal, tool_infos, params)
```

工具信息只需提取一次（工具集在运行期间不变），可缓存在 Planner 实例中。

### 6.4 trace 集成

在 `_try_plan_and_solve` 中使用现有的 `record_span` 记录：
- `span_type="orchestrator"`, `span_name="plan_and_solve"`
- attrs: `plannable`, `task_count`, `reasoning`, `planning_duration_ms`

执行阶段由 DAGExecutor 自动记录每个 task 的 span（`span_type="dag.task"`），无需额外处理。

### 6.5 SSE 事件

新增一个 stage 事件 `"plan_generated"`，在 Planning 完成后发布：

```python
_publish_stage_safe(
    "plan_generated",
    {
        "plannable": plan.plannable,
        "task_count": len(plan.tasks),
        "reasoning": plan.reasoning,
    },
    duration_ms=planning_duration_ms,
)
```

---

## 7. 失败处理与降级策略

### 7.1 失败场景与处理

| 失败场景 | 处理方式 | 降级路径 |
|----------|---------|---------|
| Planning LLM 调用超时 | `asyncio.wait_for` 超时捕获 | 降级到 ReAct |
| Planning LLM 返回非法 JSON | JSON 解析异常捕获 | 降级到 ReAct |
| `plannable=false` | LLM 判断查询需要动态决策 | 降级到 ReAct |
| 计划校验失败（工具名不存在） | `validate_plan` 返回错误列表 | 降级到 ReAct |
| 计划校验失败（循环依赖） | `validate_plan` 检测拓扑环 | 降级到 ReAct |
| DAGExecutor 执行失败 | DAGExecutor 内部异常捕获 | 返回错误结果（与静态 DAG 失败一致） |
| ReportAgent 生成失败 | ReportAgent 内部重试 + 异常 | 返回工具输出的原始数据 |

### 7.2 降级流程

```
_try_plan_and_solve()
  ├─ Planning 成功 + 校验通过 → DAGExecutor 执行 → 返回结果
  └─ 任何失败 → 返回 None → 调用方走 ReAct 兜底
```

**核心原则**：Plan and Solve 的任何失败都不应比现状更差。失败时降级到 ReAct，即回到当前的默认行为。

### 7.3 可观测性

所有失败都记录到 trace span 和日志：

```python
# 失败时的 trace 记录
with record_span("orchestrator", "plan_and_solve") as attrs:
    attrs["status"] = "fallback_to_react"
    attrs["fallback_reason"] = "planning_timeout" / "invalid_json" / "not_plannable" / "validation_failed"
    attrs["planning_duration_ms"] = ...
```

这使得可以通过 `/traces` API 统计 Plan and Solve 的命中率、失败原因分布，为后续优化提供数据。

### 7.4 开关控制

`plan_and_solve.enabled = false` 时，整个 Plan and Solve 路径被跳过，所有 L3 查询直接走 ReAct，与改造前完全一致。这是上线后的安全回退手段。

---

## 8. 性能分析与预期收益

### 8.1 LLM 调用次数对比

以 "查询最近的异常采购订单" 为例：

**ReAct 路径（当前）**：

| 步骤 | 类型 | 耗时估计 |
|------|------|---------|
| LLM 推理 #1 | 选择 query_purchase_orders | 1-2s |
| 工具执行 #1 | SQL 查询 | 0.5s |
| LLM 推理 #2 | 选择 run_three_way_match | 1-2s |
| 工具执行 #2 | 规则检测 | 1-2s |
| LLM 推理 #3 | 生成最终回复 | 2-3s |
| **合计** | **3 次 LLM + 2 次工具** | **6-9s** |

**Plan and Solve 路径（改造后）**：

| 步骤 | 类型 | 耗时估计 |
|------|------|---------|
| Planning LLM（fast） | 生成 2 步计划 | 0.5-1s |
| 工具执行 #1 + #2 | **并行** SQL + 规则检测 | 1-2s |
| ReportAgent LLM（fast） | 汇总生成报告 | 1-2s |
| **合计** | **2 次 LLM + 2 次工具（并行）** | **2.5-5s** |

### 8.2 Token 消耗对比

**ReAct 路径**：
- 每轮 LLM 调用携带：system prompt (~2000 tokens) + 工具 schema (~3000 tokens) + 对话历史（递增）
- 3 轮合计输入 token：~15000-20000
- 输出 token：~2000

**Plan and Solve 路径**：
- Planning 调用：工具摘要 (~1000 tokens) + 用户查询 (~100 tokens) = ~1100 输入，~300 输出
- ReportAgent 调用：工具输出 (~2000 tokens) + prompt (~500 tokens) = ~2500 输入，~1500 输出
- 合计：~3600 输入 + ~1800 输出

**Token 节省：约 60-70%**

### 8.3 预期收益汇总

| 指标 | ReAct（当前） | Plan and Solve | 改善 |
|------|-------------|----------------|------|
| LLM 调用次数 | 2-4 次 | 2 次 | 减少 50-60% |
| 端到端延迟 | 6-9s | 2.5-5s | 减少 40-55% |
| Token 消耗 | ~17000-22000 | ~5400 | 减少 60-70% |
| 工具执行 | 串行 | 可并行 | 取决于查询 |

### 8.4 不适用场景的性能影响

对于需要动态决策的查询（plannable=false），会多一次 Planning LLM 调用（fast 模型，~0.5-1s），然后降级到 ReAct。这是 Plan and Solve 引入的唯一额外开销。

通过 fast 模型 + 30s 超时限制，这个开销在可接受范围内。

---

## 9. 实施计划

### 9.1 分阶段实施

#### Phase 1：基础设施（核心模块）

| 步骤 | 内容 | 涉及文件 |
|------|------|---------|
| 1.1 | 新增 PlanAndSolveSettings 配置类 | `config/settings.py` |
| 1.2 | config.yaml 新增 plan_and_solve 段 | `config/config.yaml` |
| 1.3 | 新增 Planner 类（含 ExecutionPlan 数据结构） | `core/orchestrator/planner.py`（新文件） |
| 1.4 | 新增 ToolInfo 提取和格式化工具 | `core/orchestrator/planner.py` |
| 1.5 | 编写 Planning Prompt 模板 | `core/orchestrator/planner.py` |

#### Phase 2：集成（Orchestrator 改造）

| 步骤 | 内容 | 涉及文件 |
|------|------|---------|
| 2.1 | Orchestrator 新增 `_lazy_planner` 属性 | `core/orchestrator/orchestrator.py` |
| 2.2 | Orchestrator 新增 `_try_plan_and_solve` 方法 | `core/orchestrator/orchestrator.py` |
| 2.3 | 改造 `_analyze_inner` 路由决策段（第 716 行） | `core/orchestrator/orchestrator.py` |
| 2.4 | trace span 和 SSE 事件集成 | `core/orchestrator/orchestrator.py` |

#### Phase 3：测试与调优

| 步骤 | 内容 | 涉及文件 |
|------|------|---------|
| 3.1 | Planner 单元测试 | `tests/unit/test_planner.py`（新文件） |
| 3.2 | Orchestrator Plan and Solve 分支集成测试 | `tests/unit/test_orchestrator_plan.py`（新文件） |
| 3.3 | Planning Prompt 调优（基于真实查询） | `core/orchestrator/planner.py` |
| 3.4 | 性能基准测试（ReAct vs Plan and Solve） | `tests/integration/` |

### 9.2 涉及文件完整清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/orchestrator/planner.py` | **新增** | Planner 类、ExecutionPlan、ToolInfo、Planning Prompt |
| `core/orchestrator/orchestrator.py` | **修改** | 新增 _lazy_planner、_try_plan_and_solve、路由决策改造 |
| `config/settings.py` | **修改** | 新增 PlanAndSolveSettings 类 + Settings 注册 |
| `config/config.yaml` | **修改** | 新增 plan_and_solve 配置段 |
| `tests/unit/test_planner.py` | **新增** | Planner 单元测试 |
| `tests/unit/test_orchestrator_plan.py` | **新增** | Plan and Solve 集成测试 |
| `modules/p2p/prompts.py` | **不修改** | 工具速查表复用，无需改动 |
| `core/orchestrator/dag/executor.py` | **不修改** | 直接复用 |
| `core/orchestrator/dag/registry.py` | **不修改** | 直接复用 |
| `modules/p2p/report_agent.py` | **不修改** | 直接复用 |

---

## 10. 测试策略

### 10.1 单元测试（`tests/unit/test_planner.py`）

**Planning Prompt 格式化测试**：
- 工具信息正确提取和格式化
- 不同模式（postgresql / hybrid）的工具清单差异

**ExecutionPlan 解析测试**：
- 合法 JSON 正确解析为 ExecutionPlan
- plannable=false 的计划正确识别
- 非法 JSON（缺字段、格式错误）返回解析失败

**计划校验测试**：
- 工具名合法性：存在的工具通过，不存在的工具报错
- 依赖关系校验：无循环依赖通过，有循环依赖报错
- depends_on 引用的 task_id 存在性

**模型选择测试**：
- `use_fast_model=true` 时使用 `settings.llm_fast`
- `use_fast_model=false` 时使用 `settings.llm`

### 10.2 集成测试（`tests/unit/test_orchestrator_plan.py`）

**路由分支测试**：
- `plan_and_solve.enabled=false` 时所有 L3 查询走 ReAct（与现状一致）
- `plan_and_solve.enabled=true` 时 L3 查询先尝试 Plan and Solve
- 静态 DAG 命中时不走 Plan and Solve
- RECALL 意图不走 Plan and Solve

**降级测试**：
- Planning 超时 → 降级到 ReAct
- plannable=false → 降级到 ReAct
- 计划校验失败 → 降级到 ReAct
- 降级后结果与直接走 ReAct 一致

**端到端流程测试**：
- "查询最新的 PO" → Plan and Solve → DAGExecutor → ReportAgent → 结果
- "对比两个供应商" → Plan and Solve（并行两个 KPI 计算）→ 结果

### 10.3 性能基准测试

使用相同的查询集，分别在 `plan_and_solve.enabled=true/false` 下运行，对比：
- 端到端延迟（P50 / P90 / P99）
- LLM 调用次数
- Token 消耗（输入 + 输出）
- Plan and Solve 命中率（plannable=true 的比例）
- Plan and Solve 降级率（校验失败 / 超时的比例）

### 10.4 测试 fixture

复用现有的测试基础设施（`tests/conftest.py` + `tests/fixtures/p2p.py`）：
- `p2p_settings` fixture：注入 `plan_and_solve` 配置
- `mock_llm` fixture：mock Planning LLM 返回固定 JSON
- `tool_registry` fixture：注入测试用 ToolRegistry

---

## 附录：术语表

| 术语 | 说明 |
|------|------|
| Plan and Solve | 两阶段 LLM 推理模式：先生成执行计划，再按计划执行 |
| ReAct | Reasoning + Acting 循环模式：每步都由 LLM 决策 |
| DAG | 有向无环图，用于表示任务依赖关系和并行执行 |
| Planning | Plan and Solve 的第一阶段：LLM 生成结构化执行计划 |
| Solving | Plan and Solve 的第二阶段：按计划执行工具调用 |
| 降级 | Plan and Solve 失败时回退到 ReAct 路径 |
| plannable | LLM 判断查询是否可以提前规划（vs 需要动态决策） |
