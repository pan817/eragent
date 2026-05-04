# 测试体系审计报告与改善方案

> 审计日期：2026-05-03 | 分支：feature-plan-and-solve
> 范围：tests/ 全量 75 个测试文件，1549 个测试函数

---

## 1. 全景数据

### 1.1 按功能域分布

| 功能域 | 文件数 | 用例数 | 占比 | 含真实中文业务查询 |
|--------|--------|--------|------|-------------------|
| 记忆（长期/短期/session/chat） | 11 | 272 | 17.6% | 7 文件 |
| 编排/DAG/Planner | 4 | 159 | 10.3% | 3 文件 |
| ETL（Graphiti/Pipeline） | 6 | 119 | 7.7% | 0 文件 |
| 图查询（Neo4j/向量） | 3 | 85 | 5.5% | 3 文件 |
| 可观测性（trace/log） | 3 | 78 | 5.0% | 1 文件 |
| 配置/模型工厂 | 3 | 72 | 4.6% | 1 文件 |
| API/HTTP 路由 | 6 | 62 | 4.0% | 1 文件 |
| 集成测试 | 8 | 51 | 3.3% | 4 文件 |
| 意图路由 | 2 | 42 | 2.7% | 2 文件 |
| Lookup 快捷查询 | 1 | 40 | 2.6% | 1 文件 |
| 数据查询（Repository/工具） | 4 | 35 | 2.3% | 2 文件 |
| 报告���成 | 3 | 24 | 1.5% | 0 文件 |
| 其他（加密/事件/规则等） | 21 | 410 | 26.5% | 7 文件 |
| **合计** | **75** | **1549** | **100%** | **32 文件** |

### 1.2 关键文件 Mock 率量化

> mock_refs = 文件中 `Mock / AsyncMock / MagicMock / patch` 出现次数；mock 率 = mock_refs / (test_count * 3) 的经验估算

| 文件 | 用例数 | mock_refs | mock 率评级 | 说明 |
|------|--------|-----------|------------|------|
| `test_orchestrator.py` | 90 | 90 | **极高** | 每个测试都 patch intent_router.route()，agent 全部 AsyncMock |
| `test_short_term_memory.py` | 37 | 91 | **极高** | checkpointer/LLM/agent 全部 mock，仅 entity engine 真实 |
| `test_dag.py` | 46 | 51 | **高** | 模板加载真实，但 DAG 执行中的工具全部 AsyncMock |
| `test_long_term_memory.py` | 87 | 32 | **中** | 核心 CRUD 真实（SQLite），向量检索部分 mock |
| `test_planner.py` | 23 | 10 | **中** | LLM 调用 mock，验证解析逻辑真实 |
| `test_lookup.py` | 40 | 9 | **低** | 路由决策真实，仅 execute_lookup 的工具调用 mock |
| `test_memory_integration.py` | 15 | 6 | **低** | 全链路真实 SQLite 操作 |
| `test_repository.py` | 16 | 4 | **极低** | 真实 SQL 查询 + SQLite 内存库 |
| `test_router.py` | 30 | 4 | **极低** | 正则提取和 bypass 分类全真实，仅 LLM 路由 mock |
| `test_tools.py` | 20 | 0 | **无** | 真实工具调用，但断言极弱（见 2.1） |

### 1.3 端到端/集成测试真实度

| 文件 | 用例数 | 真实 LLM | 真实工具 | 真实 DB | 评估 |
|------|--------|---------|---------|--------|------|
| `test_e2e.py` | 16 | Qwen3-max | 真实 | SQLite mock 数据 | **唯一真实 E2E** |
| `test_dual_schema.py` | 24 | 否 | 真��� repo | SQLite mock 数据 | 仅验证双 schema 契约 |
| `test_api.py` | 9 | 否 | 否 | 否 | 仅验证 HTTP 路由绑定 |
| `test_sse_react_chunk_flow.py` | 2 | 否 | 否 | 否 | 仅验证 SSE 协议 |
| `test_async_analyze_integration.py` | 3 | 否 | 否 | 否 | 仅验证异步任务生命周期 |
| `test_plan_and_solve.py` | 9 | 否 | 否 | 否 | 全部 mock，仅验证路由分支 |
| `test_agent.py` | 4 | 否 | 否 | 否 | agent chain 全 mock |
| `test_async_multi_worker.py` | 4 | 否 | 否 | fakeredis | 仅验证 EventBus |

---

## 2. 三类问题诊断

### 2.1 问题 A：无效测试（测 mock 而非测逻辑）

**定义**：测试通过 mock 注入了预设返回值，断言只验证 mock 的返回值被正确传递，而非验证真实业务逻辑。这类测试即使被测代码有严重 bug，也永远通过。

#### A-1. test_orchestrator.py — 路由决策全部注入，从未真实路由

```python
# 典型模式（第 123 行）：
with patch.object(orch._intent_router, "route", return_value=_make_l3_signal()):
    result = await orch.analyze(query="任意文字", ...)
```

- 90 个测试中，**每一个**都 `patch.object(orch._intent_router, "route", ...)` 注入预设信号
- 测试验证的是"收到 L3 信号后走 ReAct"，但从未验证"真实查询是否会产生 L3 信号"
- agent.run 全部 `AsyncMock(return_value=预设结果)`，不执行真实工具调用
- **后果**：路由规则有 bug（如漏匹配关键词）→ 测试无法发现

#### A-2. test_tools.py — 断言只验证类型，不验证内容

```python
# 第 37-41 行：
async def test_query_purchase_orders(self) -> None:
    result = await query_purchase_orders.ainvoke({"vendor_id": "", "status": "", "days": 30})
    data = json.loads(result)
    assert isinstance(data, list)  # 空列表也通过！
```

- 20 个工具测试，查询类（4 个）的断言全部是 `isinstance(data, list)`
- **从未验证**：返回的记录数是否 > 0、字段值是否正确、过滤条件是否生效
- `limit` / `order_by` 参数完全没有测试覆盖
- **后果**：`order_by=date_desc` 实现写反了 → 测试通过；返回空列表 → 测试通过

#### A-3. test_dag.py — 执行层全 mock，只验证调度

```python
# 第 291-310 行：
tool = AsyncMock(return_value='[{"po": "PO-001"}]')  # 硬编码返回值
registry = ToolRegistry()
registry.register("query_purchase_orders", tool)
results = await executor.execute(dag, registry)
assert results["fetch_po"].output == '[{"po": "PO-001"}]'  # 验证 mock 返回值被传递
```

- DAG 模板加载测试（13 个）是真实的
- 但 DAG 执行测试（10 个）的工具全部 AsyncMock，验证的是"调度器把 mock 返回值传给了下一个 mock"
- **后果**：真实工具参数传递错误、工具间数据格式不匹配 → 测试无法发现

#### A-4. test_e2e.py — 断言过宽，无法捕捉结果正确性

```python
# 第 94-104 行：
assert resp.status_code == 200
assert data["status"] == "success"
assert len(data["report_markdown"]) > 50  # 只要有内容就通过
```

- E2E 测试使用真实 LLM，是最有价值的测试
- 但断言仅验证 `status=success` + `report 长度 > 50`
- **从未验证**：报告是否包含预期的业务数据、是否调用了正确的工具、数值是否准确
- **后果**：LLM 返回一段无关的长文本 → 测试通过；查询正确但数据全错 → 测试通过

### 2.2 问题 B：用例不足（关键路径无覆盖）

| 缺失场景 | 涉及代码 | 风险等级 |
|---------|---------|---------|
| Lookup shortcut 返回空结果时不 fallback | `orchestrator.py:1034-1066` | **P0** — 已造成真实 bug |
| 工具 `limit` + `order_by` 参数联合行为 | `repository.py:106-110` | **P0** — 直接影响"最新 N 个"查询 |
| 真实中文查询 → 意图路由准确性 | `unified_router.py` + `router/__init__.py` | **P0** — LLM 路由是核心路径 |
| 高频简单查询（"最新的一个po"等） | 全链路 | **P0** — 用户最常用场景 |
| 工具间数据契约（DAG task A 输出 → task B 输入） | `dag/executor.py` | **P1** — DAG 并行执行的基础假设 |
| Plan and Solve planner 生成的 DAG 合法性 | `planner.py` → `dag/validator.py` | **P1** — PS 路径核心 |
| 多轮对话代词引用（"上一个"、"那个供应商"） | `orchestrator.py` 引用解析 | **P1** — 生产常见场景 |
| 报告内容质量（含预期关键词/数值） | `report_agent.py` | **P2** — 影响用户信任 |
| 记忆注入对分析结果的影响 | `memory/injection.py` → agent | **P2** — 记忆功能的实际效果 |
| SSE 流式输出在真实 LLM 下的行为 | `test_e2e.py` 仅 1 个流式测试 | **P2** — 前端体验 |

### 2.3 问题 C：体系设计缺陷

#### C-1. 分层只按执行方式，未按功能域

当前结构：
```
tests/
├── unit/           # 67 个文件，按被测文件名命名
├── integration/    # 8 个文件
└── fixtures/       # 1 个 fixture 文件
```

**问题**：
- 要了解"意图路由"的测试覆盖情况，需翻 `test_router.py` + `test_orchestrator.py` + `test_lookup.py` + `test_e2e.py` + `test_plan_and_solve.py` 共 5 个文件
- 要了解"查询工具"的测试覆盖，需翻 `test_tools.py` + `test_tools_pg_sql.py` + `test_repository.py` + `test_dual_schema.py`
- 新增功能时不知道该加在哪个文件

#### C-2. 单元测试与集成测试边界模糊

- `test_memory_integration.py` 放在 `unit/` 但实际是集成测试
- `test_plan_and_solve.py` 放在 `integration/` 但全部 mock，实际是单元测试
- `test_dual_schema.py` 放在 `integration/` 但 repo 测试部分是单元测试级别

#### C-3. fixture 体系薄弱

- 全局 `conftest.py` 仅提供 `settings` + `db_engine`（50 条 mock 数据）
- E2E 的 `e2e_client` fixture 在 `test_e2e.py` 内部定义，无法复用
- 各测试文件自建 mock provider/agent，模式不统一
- 没有"标准查询集 fixture"供多个测试文件共享

#### C-4. 覆盖率指标误导

当前 90.36% 覆盖率 = 代码行被执行过，但：
- 执行时输入是 mock → 不能代表真实场景覆盖
- 断言只检查类型 → 行被执行但逻辑正确性未验证
- 覆盖率高 ≠ 质量高，给团队错误的安全感

---

## 3. 典型 Bug 路径还原："最新的一个po"

### 3.1 完整执行路径

以用户查询 `"最新的一个po"` 为例，逐步追踪生产代码，标注每一步的测试覆盖状态。

```
步骤 1 → orchestrator.analyze()                [orchestrator.py:365]
  ├─ 引用解析 resolve_references()              [orchestrator.py:464]     ✅ 有单元测试
  ├─ bypass 检测 _classify_bypass()             [router/__init__.py:333]  ✅ 有单元测试
  │   └─ "最新的一个po" 不匹配 CHITCHAT/META/RECALL → is_bypass=False
  │
步骤 2 → IntentRouter.route()                  [unified_router.py:250]
  ├─ LLM 调用 → 返回 intent_kind=DATA_LOOKUP    [unified_router.py:300]  ⚠️ LLM 行为无确定性测试
  │   signal.limit=1, signal.order_by=date_desc
  │   signal.is_cross_entity=False
  │
步骤 3 → lookup shortcut 条件判断              [orchestrator.py:742-746]
  ├─ is_data_lookup=True                                                  ❌ 未测试此组合条件
  ├─ lookup_shortcut_enabled=True
  ├─ not is_cross_entity=True
  ├─ _lookup_backend_available=True
  ├─ _lookup_output_ok=True
  │   └─ 全部满足 → 进入 lookup shortcut
  │
步骤 4 → resolve_lookup_tool()                 [lookup.py:153]
  ├─ Path A: 无 po_number/vendor_id → MISS                               ✅ 有单元测试
  ├─ Path B: 四重漏斗                                                     ✅ 有单元测试
  │   ├─ #1 实体类型："po" 命中 → query_purchase_orders
  │   ├─ #2 数量/时间："最新的一个" → limit=1, order_by=date_desc
  │   ├─ #3 黑名单词：无 → 通过
  │   └─ #4 图意图词：无 → 通过
  │   → 返回 ("query_purchase_orders", {days:30, limit:1, order_by:"date_desc"})
  │
步骤 5 → execute_lookup()                      [lookup.py:218]
  ├─ 调用 tool.ainvoke({days:30, limit:1, order_by:"date_desc"})         ❌ 无参数组合测试
  │   └─ 后端 query_purchase_orders 执行 SQL
  │
步骤 6 → repository.query_purchase_orders()    [repository.py:64]
  ├─ WHERE creation_date >= today - 30 days                               ⚠️ 仅测试 days=0/30，无 limit+order_by
  ├─ ORDER BY creation_date DESC
  ├─ LIMIT 1
  │   └─ ���关键】如果 mock 数据中无近 30 天数据 → 返回 []
  │
步骤 7 → format_lookup_result([])              [lookup.py:388]
  ├─ items 为空列表                                                       ✅ 有单元测试（test_empty_list）
  │   └─ 返回 "未找到匹配的采购订单。"
  │
步骤 8 → _try_lookup_shortcut()                [orchestrator.py:1034]
  ├─ result_md = "未找到匹配的采购订单。"                                   ❌ 未测试空结果路径
  ├─ result_md is not None → 认为 lookup 成功
  ├─ 包装为 AnalysisResult(status=SUCCESS)
  │   └─ 返回 → orchestrator 早退，不进入 PS/ReAct
  │
步骤 9 → 用户看到："未找到匹配的采购订单。"
  └─ 【BUG】数据库中明明有 PO，但 lookup shortcut 把空结果当成功返回
```

### 3.2 测试盲区分析

上述 9 步中，**步骤 3/5/6(参数组合)/8** 完全没有测试覆盖：

| 步骤 | 盲区 | 根因 |
|------|------|------|
| 步骤 3 | lookup shortcut 入口条件组合 | test_orchestrator.py 直接 mock 掉 router，不经过此判断 |
| 步骤 5 | 真实工具调用 + 参数传递 | test_lookup.py 的 execute_lookup 用 AsyncMock 工具 |
| 步骤 6 | limit + order_by + days 联合 SQL 行为 | test_repository.py 仅测 days=0 和 vendor_id 过滤 |
| 步骤 8 | 空结果不触发 fallback | test_orchestrator.py 从未构造 lookup 返回空结果的场景 |

### 3.3 Bug 根因

此 bug 有两个层面的问题：

**代码层面**：`_try_lookup_shortcut()` 在 `orchestrator.py:1034` 判断 `result_md is None` 才认为 miss。但 `format_lookup_result()` 在空结果时返回 `"未找到匹配的采购订单。"`（非 None），被误判为成功。

**测试层面**：没有任何测试覆盖"lookup 工具返回空列表时 orchestrator 的行为"。test_lookup.py 的 `test_empty_list` 只测了 `format_lookup_result` 的格式化输出，没有测 orchestrator 收到这个输出后是否应该 fallback。

---

## 4. 改善方案：新测试分层架构 + Mock 策略

### 4.1 双维度测试组织

**现状**：只按执行类型（unit / integration）分目录。
**目标**：按 **功能域 × 测试深度** 双维度组织。

```
tests/
├── conftest.py                          # 全局 fixture（Settings, DB engine, mock 数据）
├── fixtures/
│   ├── p2p.py                           # P2P 静态 fixture（已有）
│   ├── queries.py                       # 【新增】标准查询集 fixture
│   └── tools.py                         # 【新增】真实工具 + 真实 repo fixture
├── unit/                                # 保持现有，逐步增强断言
│   └── (现有 67 文件不动)
├── functional/                          # 【新增】功能域集成测试
│   ├── test_routing_accuracy.py         # 意图路由：真实查询→路由结果
│   ├── test_lookup_pipeline.py          # Lookup 全链路：查询→工具→格式化→fallback
│   ├── test_dag_pipeline.py             # DAG 全链路：模板→工具执行→报告
│   ├── test_tool_correctness.py         # 工具正确性：参数组合→数据验证
│   ├── test_memory_effect.py            # 记忆效果：注入→影响分析结果
│   └── test_multi_turn.py              # 多轮对话：代词引用、追问
├── integration/                         # 保持现有，补充 SSE/async 真实测试
│   └── (现有 8 文件不动)
├── regression/                          # 【新增】回归测试
│   └── test_canary_queries.py           # 金丝雀查询集：高频查询→预期结果
└── http/                                # 手动 REST 测试文件（已有）
```

### 4.2 各层测试职责定义

| 层级 | 目的 | Mock 策略 | 运行频率 |
|------|------|----------|---------|
| **unit/** | 验证单个函数/类的内部逻辑 | 可 mock 外部依赖（LLM、外部 DB） | 每次 commit |
| **functional/** | 验证功能域端到端正确性 | 只 mock LLM，其余（DB、工具、路由逻辑）全真实 | 每次 commit |
| **integration/** | 验证组件间交互 + HTTP 层 | 尽量真实 | 每次 PR |
| **regression/** | 用户高频查询不退化 | 只 mock LLM（固定返回值） | 每次 PR |
| **e2e/** (已有) | 验证完整链路含 LLM | 全真实（需 API Key） | 发版前 |

### 4.3 Mock 策略重新定义

**原则**：Mock 的边界是 LLM 调用和外部服务（Neo4j/Redis/真实 PG），其余全部用真实代码 + SQLite 内存库。

| 组件 | 单元测试 | functional 测试 | E2E 测试 |
|------|---------|----------------|---------|
| LLM（Qwen/GPT） | Mock（固定返回） | Mock（固定返回） | **真实** |
| IntentRouter 正则/bypass | **真实** | **真实** | **真实** |
| UnifiedRouter LLM 调用 | Mock | Mock（注入固定信号） | **真实** |
| Repository SQL | **真实**（SQLite） | **真实**（SQLite） | **真实**（SQLite） |
| Tool 函数 | **真实** | **真实** | **真实** |
| DAG Executor 调度 | Mock 工具 | **真实工具** | **真实** |
| Report Agent | Mock LLM | Mock LLM | **真实** |
| Memory CRUD | **真实**（SQLite） | **真实**（SQLite） | **真实**（SQLite） |
| Neo4j/Graphiti | Mock | Mock | Mock（除非有测试实例） |

**关键改变**：
- `functional/` 层的工具调用**必须真实**，不再 AsyncMock
- 路由决策测试**必须用真实查询**，不再��入预设信号
- 断言**必须验证内容正确性**，不仅验证类型

### 4.4 标准查询集 fixture 设计

新增 `tests/fixtures/queries.py`，集中管理查询→预期行为的映射：

```python
CANARY_QUERIES = [
    # (query, expected_route, expected_tool, min_result_count, must_contain_fields)
    ("最新的一个po", "lookup_shortcut", "query_purchase_orders", 1, ["po_number"]),
    ("最新的5个PO", "lookup_shortcut", "query_purchase_orders", 5, ["po_number"]),
    ("金额最大的3笔付款", "lookup_shortcut", "query_payments", 3, ["amount"]),
    ("查询最近7天的发票", "lookup_shortcut", "query_invoices", 1, ["invoice_num"]),
    ("查看PO-001", "lookup_shortcut", "query_purchase_orders", 1, ["po_number"]),
    ("分析三路匹配异常", "DAG", "run_three_way_match", None, None),
    ("分析采购价格差异", "DAG", "run_price_variance_analysis", None, None),
    ("你好", "bypass", None, None, None),
    ("你能做什么", "bypass", None, None, None),
    ("帮我全面分析一下", "plan_and_solve|agent", None, None, None),
]
```

### 4.5 需要增强的现有测试文件

| 现有文件 | 当前问题 | 改善动作 |
|---------|---------|---------|
| `test_tools.py` | 断言仅 `isinstance(data, list)` | 增加：result_count > 0、字段值验证、limit/order_by 行为 |
| `test_repository.py` | 仅 16 个测试，无 limit+order_by | 增加：参数组合测试、排序验证、边界值 |
| `test_orchestrator.py` | 全部 mock router | 增加 functional/ 层对应测试，保留 unit/ 层现有测试 |
| `test_e2e.py` | 断言过宽 | 增加 report 内容关键词断言、route_type 精确断言 |
| `test_lookup.py` | execute_lookup mock 工具 | 增加 functional/ 层真实工具调用测试 |

---

## 5. 功能域场景矩阵 + 金丝雀查询集

### 5.1 功能域场景矩阵

#### 域 1：意图路由

| 场景 | 预期路由 | 现有覆盖 | 优先级 |
|------|---------|---------|--------|
| 闲聊："你好"/"谢谢" | bypass → CHITCHAT | ✅ test_router.py | - |
| 能力询问："你能做什么" | bypass → META | ✅ test_router.py | - |
| 回忆："上次分析的结果" | bypass → RECALL | ✅ test_router.py | - |
| 精确查询："最新的一个po" | DATA_LOOKUP → lookup shortcut | ❌ 无真实 LLM 路由测试 | **P0** |
| L1 关键词命中："三路匹配分析" | ANALYSIS → DAG | ⚠️ 仅 mock 信号 | **P0** |
| L2 语义匹配："付款有没有逾期" | ANALYSIS → DAG | ⚠️ 仅 mock 信号 | **P1** |
| L3 兜底："帮我看看采购数据" | ANALYSIS → PS/ReAct | ⚠️ 仅 mock 信号 | **P1** |
| 模糊+实体："SUP-001 最近的订单" | DATA_LOOKUP + entity | ❌ | **P1** |
| 跨实体："PO 和发票的对比" | ANALYSIS (cross_entity) | ❌ | **P2** |

#### 域 2：Lookup 快捷查询

| 场景 | 预期行为 | 现有覆盖 | 优先级 |
|------|---------|---------|--------|
| "最新的一个po" → 有数据 | 返回 1 条 PO markdown | ❌ 无真实执行 | **P0** |
| "最新的一个po" → 无数据 | fallback 到 PS/ReAct | ❌ 代码+测试都缺 | **P0** |
| "最新的5个PO" → limit=5 | 返回 5 条 PO 按时间倒序 | ❌ 无参数组合测试 | **P0** |
| "金额最大的3笔付款" → order_by=amount_desc | 返回 3 笔按金额倒序 | ❌ 无排序验证 | **P0** |
| "查看PO-001" → Path A 精确匹配 | 返回 PO-001 详情 | ⚠️ 路由有，执行无 | **P1** |
| "查询最近7天的发票" → 时间窗 | 返回 7 天内发票 | ⚠️ 路由有，执行无 | **P1** |
| "本月的供应商列表" → vendor_master | 返回供应商列表 | ⚠️ 路由有，执行无 | **P1** |
| 工具抛异常 → 降级处理 | 返回 None → fallback | ✅ test_lookup.py | - |

#### 域 3：DAG 执行

| 场景 | 预期行为 | 现有覆盖 | 优先级 |
|------|---------|---------|--------|
| 三路匹配 DAG → 全部工具成功 | 并行执行 + 报告汇总 | ❌ 工具全 mock | **P0** |
| DAG 某工具失败 → 级联处理 | 依赖任务标记失败，其余继续 | ✅ mock 级联测试 | - |
| PS 生成 DAG → 执行 | 动态 DAG 合法且可执行 | ❌ 无真实 PS → 执行链路 | **P1** |
| DAG 工具 A 输出 → 工具 B 输入 | 数据格式兼容 | ❌ 无数据契约测试 | **P1** |
| Report Agent 汇总结果 | 报告含预期业务关键词 | ❌ mock LLM | **P2** |

#### 域 4：数据查询工具

| 场景 | 预期行为 | 现有覆盖 | 优先级 |
|------|---------|---------|--------|
| query_purchase_orders(days=30) | 返回近30天PO | ✅ 但断言弱 | **P0 增强** |
| query_purchase_orders(limit=1, order_by=date_desc) | 返回最新1条 | ❌ | **P0** |
| query_purchase_orders(vendor_id=X) | 仅返回该供应商 | ✅ test_repository.py | - |
| query_purchase_orders(days=30) → 无数据 | 返回空列表 | ❌ | **P0** |
| run_three_way_match(po_number=X) | 返回匹配结果 | ⚠️ 断言弱 | **P1** |
| calculate_supplier_kpis(vendor_id=X) | 返回 KPI dict | ⚠️ 断言弱 | **P1** |

#### 域 5：记忆系统

| 场景 | 预期行为 | 现有覆盖 | 优先级 |
|------|---------|---------|--------|
| 分析后写入实体记忆 | memory 表有记录 | ✅ test_memory_integration.py | - |
| 记忆注入到 prompt | 影响分析结果 | ❌ | **P2** |
| session recap 跨会话回忆 | 回忆到上次内容 | ⚠️ test_recall_e2e.py 部分覆盖 | **P2** |
| 记忆 TTL 过期 + consolidation | 自动清理/合并 | ✅ | - |

### 5.2 金丝雀查询集（回归基线）

以下 20 条查询从真实用户高频模式提取，覆盖所有路由路径：

| # | 查询 | 预期路由 | 预期工具 | 关键断言 |
|---|------|---------|---------|---------|
| 1 | 最新的一个po | lookup | query_purchase_orders | result_count=1, 有 po_number |
| 2 | 最新的5个PO | lookup | query_purchase_orders | result_count=5, 按日期倒序 |
| 3 | 金额最大的3笔付款 | lookup | query_payments | result_count=3, 金额递减 |
| 4 | 查询最近7天的发票 | lookup | query_invoices | 所有日期在7天内 |
| 5 | 查看PO-001 | lookup | query_purchase_orders | po_number=PO-001 |
| 6 | SUP-001的采购订单 | lookup | query_purchase_orders | 所有 vendor_id=SUP-001 |
| 7 | 本月的供应商列表 | lookup | query_vendor_master | result_count >= 1 |
| 8 | 查一下最近的收货单 | lookup | query_receipts | result_count >= 1 |
| 9 | 分析三路匹配异常 | DAG | run_three_way_match | analysis_type=three_way_match |
| 10 | 分析采购价格差异 | DAG | run_price_variance | analysis_type=price_variance |
| 11 | 检查付款合规性 | DAG | run_payment_compliance | analysis_type=payment_compliance |
| 12 | 评估SUP-001的绩效 | DAG | calculate_supplier_kpis | analysis_type=supplier_performance |
| 13 | 帮我全面分析采购数据 | PS/agent | 多个 | report 长度>200 |
| 14 | 查询最近7天采购订单概况 | PS | query_purchase_orders | report 含数量统计 |
| 15 | 你好 | bypass | 无 | CHITCHAT 模板响应 |
| 16 | 你能做什么 | bypass | 无 | META 模板响应 |
| 17 | 上次分析了什么 | bypass | 无 | RECALL 响应 |
| 18 | 列出最近7天的采购订单和发票 | PS/agent | 多工具 | 跨实体，非 lookup |
| 19 | 为什么最新的PO金额这么高 | PS/agent | query + analyze | 非 lookup（黑名单词"为什么"） |
| 20 | 最近付款合规情况 | DAG/PS | run_payment_compliance | 非 lookup（黑名单词"合规"） |

### 5.3 实施优先级路线图

#### Phase 1（1-2 天）— 止血：修复已知 bug + 补 P0 测试

1. **修复 lookup 空结果不 fallback 的 bug**（`orchestrator.py:1034`）
2. **增强 test_tools.py**：为 4 个查询工具增加 limit + order_by 参数测试，断言 result_count 和排序
3. **增强 test_repository.py**：增加 limit + order_by 联合 SQL 行为测试
4. **新增 test_lookup_pipeline.py（functional/）**：真实工具 + 真实 repo 测试 lookup 全链路

#### Phase 2（2-3 天）— 补全功能域测试

5. **新增 test_routing_accuracy.py（functional/）**：用固定 LLM mock 返回值测试 20 条金丝雀查询的路由准确性
6. **新增 test_tool_correctness.py（functional/）**：每个工具 × 关键参数组合，断言输出内容正确性
7. **新增 test_canary_queries.py（regression/）**：金丝雀查询集自动化

#### Phase 3（2-3 天）— 深化 DAG + 多轮

8. **新增 test_dag_pipeline.py（functional/）**：DAG 真实工具执行 + 工具间数据契约
9. **新增 test_multi_turn.py（functional/）**：多轮对话代词引用测试
10. **增强 test_e2e.py 断言**：报告内容关键词 + 数值范围验证

#### Phase 4（持续）— 质量基线

11. 将金丝雀查询集纳入 CI，每次 PR 自动运行
12. 建立"断言质量"review checklist：禁止 `isinstance(data, list)` 作为唯一断言
13. 定期从生产日志提取新 query 模式，扩充金丝雀集
