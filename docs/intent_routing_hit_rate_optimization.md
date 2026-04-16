# 意图路由命中率优化方案

> **专题目标**：把更多流量导到 DAG 路径，减少 ReAct 兜底触发率，提升路由命中率与可观测性。
>
> **本文档性质**：实施方案与改动定位说明，**不包含代码**。
> **代码改动跟踪**：见各批次落地后的 commit / PR。

---

## 元信息

| 项 | 值 |
|---|---|
| 创建日期 | 2026-04-16 |
| 当前版本 | v1.0 (初稿) |
| 范围 | 仅意图路由层（`core/orchestrator/router.py` + `orchestrator.py` + `dag/case_store.py` + 配套配置/测试/监控）|
| 不在范围 | LLM provider 切换、DAG 任务节点本身的实现质量、Agent ReAct prompt 优化（已有独立专题）|
| 前置工作 | 已完成 [intent_kind 二段分类](../core/orchestrator/signal.py) 改造（CHITCHAT/META/CLARIFICATION/DATA_LOOKUP/RECALL/OUT_OF_SCOPE/ANALYSIS）|

---

## 目录

1. [Context（背景与目标）](#1-context背景与目标)
2. [现状与调研结论](#2-现状与调研结论)
3. [整体方案与执行顺序](#3-整体方案与执行顺序)
4. [批次 1：阈值配置化 + 命中率监控](#4-批次-1阈值配置化--命中率监控)
5. [批次 2：L1 词库扩展](#5-批次-2l1-词库扩展)
6. [批次 3：L2 top-k 投票](#6-批次-3l2-top-k-投票)
7. [批次 4：case_store 写入闭环 + L2.5 检索](#7-批次-4case_store-写入闭环--l25-检索)
8. [批次 5：通用 DAG 模板 RECENT_PROCUREMENT_HEALTH](#8-批次-5通用-dag-模板-recent_procurement_health)
9. [不做项与 punt 决策](#9-不做项与-punt-决策)
10. [验证方法、回滚策略与文件改动定位](#10-验证方法回滚策略与文件改动定位)
11. [附录：术语表](#11-附录术语表)

---

## 1. Context（背景与目标）

### 1.1 触发背景

eragent 当前的意图路由分三级：
- **L1**：关键词命中率（[router.py `_RULE_LIBRARY`](../core/orchestrator/router.py)，10 条规则）
- **L2**：Chroma 种子库语义匹配（[intent_seeds.yaml](../config/intent_seeds.yaml)）
- **L3**：LLM 二段分类（intent_kind + analysis_type，[router.py `_LLM_CLASSIFY_PROMPT`](../core/orchestrator/router.py)）

orchestrator 拿到 `QuerySignal` 后再决定走 **DAG 模板**（结构化、快、稳）还是 **ReAct 兜底**（自由探索、慢、不可预测）。

**问题**：当前 ReAct 兜底触发率偏高，原因有三：
1. **L1 关键词库窄**——口语化表达大量漏命中（如"拖欠"漏过 `payment_compliance`、"砍价空间"漏过 `price_variance`）。
2. **L2 阈值与权重单一**——只取 top-1，单条种子噪声会直接决定结果；length_ratio 折扣 0.7 对短查询过度惩罚。
3. **L3 低置信度强制 ReAct**——`confidence < 0.5` 即兜底，但 0.3-0.5 区间其实有明确 analysis_type 提示，浪费了 DAG 模板能力。
4. **case_store 写入闭环未集成**——已实现的"成功 DAG 案例 → Chroma 案例库"自学习链路完全空转，DAGExecutor 没主动写入。
5. **COMPREHENSIVE 无实体永远走 ReAct**——"看看最近采购情况"这类高频概览查询缺少模板支撑。

### 1.2 目标指标

| 指标 | 基线（待采集） | 目标 |
|---|---|---|
| ReAct 触发率（L3 ANALYSIS 低置信 + COMPREHENSIVE 无实体之和占总流量比） | 待批 1 监控就位后采集 7 天 | **下降 30~45 pp** |
| L1 命中率（route_level=1 占总流量比） | 待采集 | **+8~15 pp** |
| L2 命中率（route_level=2 占总流量比） | 待采集 | **+5~10 pp** |
| 平均路由延迟（P50） | 待采集 | 不退化（L2.5 引入需控制在 +50 ms 内）|
| DAG 报告成功率（status=success / 总 DAG 触发） | ≥ 现有水平 | 不退化 |

### 1.3 范围与约束

**改动范围**：
- `core/orchestrator/router.py`（L1/L2/L2.5 路由逻辑）
- `core/orchestrator/orchestrator.py`（DAG 决策、监控 span、模板触发）
- `core/orchestrator/dag/case_store.py`（新增 search 接口）
- `core/orchestrator/dag/executor.py`（接入 case_store 写入）
- `core/orchestrator/dag/templates.py`（新增通用模板）
- `config/settings.py` + `config/config.yaml`（阈值配置化）
- `tests/unit/`（每批单测、覆盖率不低于 90%）
- `api/routes/admin_metrics.py`（新文件，命中率 dashboard API）
- `scripts/route_hit_rate.sql`（新文件）

**约束**：
1. **零破坏性**：每批默认配置必须等价当前硬编码行为，灰度切换激进档；不能破坏现有 1033 个测试。
2. **可观测**：每批改动都要有日志/trace span/metric，失败必须有 WARNING+，禁止静默吞异常。
3. **可回滚**：每批都要有 feature flag 或配置开关，任何一档线上回升立即关闭。
4. **不引入新依赖**：复用现有 Chroma / PostgreSQL / SQLAlchemy / Pydantic Settings。
5. **不动 case_store 数据库 schema**：[migrations/versions/0005_dag_cases.py](../migrations/versions/) 已建立，沿用。
6. **生产标准**：遵循 [CLAUDE.md](../CLAUDE.md) 的"正式生产版本"原则——配置项齐全、关键路径有测试、不留 TODO。

### 1.4 不在范围

- **L3 prompt 内容微调**（已在 [docs/prompt_issue.md](prompt_issue.md) 与 intent_kind 改造覆盖）
- **DAG 任务节点本身的实现质量**（DAG executor / 各 task handler 不动）
- **ReAct Agent 的 prompt / tool 集**（独立专题）
- **长期记忆 / 短期记忆策略**（独立专题，已有 [docs/long_term_memory_refactor.md](long_term_memory_refactor.md)）
- **API 层 schema / 鉴权 / 限流**（不影响路由）

---

## 2. 现状与调研结论

调研覆盖 6 个维度（DAG 路径触发条件、模板覆盖面、可观测性、case_store 闭环、可配置阈值、L3 兜底策略）。结论按维度汇总如下。

### 2.1 DAG 路径触发条件

`use_dag` 决策位于 [orchestrator.py:1067-1096](../core/orchestrator/orchestrator.py)，逻辑：

```
强制 ReAct：
  - intent_kind == RECALL（回溯历史会话）
  - intent_kind == DATA_LOOKUP（纯事实查询）
  - intent_kind == ANALYSIS 且 route_level == 3 且 confidence < 0.5（低置信兜底）

走 DAG：
  - signal.route_level ∈ {1, 2} 且 analysis_type != COMPREHENSIVE
  - analysis_type == COMPREHENSIVE 且 has_entity（po_number/supplier_id/...任一）

其余 → ReAct 兜底
```

**关键观察**：
- `0.5` 阈值硬编码，无法配置或灰度
- `COMPREHENSIVE 无实体` 是无声的最大 ReAct 流量入口（"看看最近采购"这类高频概览查询）
- L3 confidence ∈ [0.3, 0.5) 区间内，LLM 其实给出了具体 analysis_type 提示，但被一刀切丢给 ReAct

### 2.2 DAG 模板覆盖面

[dag/templates.py](../core/orchestrator/dag/templates.py) 当前模板：

| 类别 | 数量 | 列表 |
|---|---|---|
| AnalysisType 静态模板 | 10 | THREE_WAY_MATCH, PRICE_VARIANCE, PAYMENT_COMPLIANCE, SUPPLIER_PERFORMANCE, SPEND_ANALYSIS, RECEIPT_ANOMALY, INVOICE_DUPLICATE, DISCOUNT_UTILIZATION, PO_CYCLE_TIME, VENDOR_CONCENTRATION |
| 实体维度模板 | 4 | payment_single, invoice_single, po_risk, supplier_risk |
| **缺失** | — | **COMPREHENSIVE 无实体的"概览类"无任何模板**（直接落 ReAct）|

### 2.3 可观测性现状

trace span 结构：
- 父 span：`intent.route_decision` （含 hit_level/result_type/confidence/reasoning）
- 子 span：`intent.l1` / `intent.l2` / `intent.l3`
- SSE stage 事件：`intent_resolved`

**已有数据源**：[trace_spans](../core/observability/tables.py) 表 + `attributes` JSONB 字段，可 SQL 聚合。

**缺失**：
- 没有 `execution=dag|react` 属性（DAG vs ReAct 分布需要从两类不同 span 名间接推断）
- 没有命中率 dashboard / API（每次要手写 SQL 才能看分布）
- 没有 metrics（Prometheus / OpenTelemetry counter）

### 2.4 case_store 闭环现状

[dag/case_store.py](../core/orchestrator/dag/case_store.py) 当前能力：

| 能力 | 现状 | 缺口 |
|---|---|---|
| `store_successful_case(query, analysis_type, dag, route_type, exec_result)` | ✅ 实现完整（PG 权威 + Chroma 缓存）| ❌ **生产代码无任何 caller**——`grep` 显示只有 `tests/unit/test_dag*.py` 调用 |
| `load_to_chroma_on_startup()` | ✅ 实现完整（服务启动时从 PG 拉到 Chroma）| 等案例库非空才有效 |
| `search_similar_case(...)` | ❌ **不存在** | L2.5 检索的核心 API 缺失 |

**结论**：自学习闭环骨架完整，但三个缺口要补——写入 caller、search API、L2.5 接入。

### 2.5 可配置阈值现状

| 阈值 | 当前值 | 位置 | 性质 |
|---|---|---|---|
| L1 默认 hit_rate threshold | 0.15 | `_RULE_LIBRARY` 各 dict | 硬编码 |
| L1 严格 hit_rate threshold | 0.20 | `_RULE_LIBRARY` 各 dict | 硬编码（INVOICE_DUPLICATE / VENDOR_CONCENTRATION）|
| L2 similarity 阈值 | 0.80 | `router.py:733` | 硬编码常量 |
| L2 length_ratio 折扣触发 | 0.4 | `router.py:177` | 硬编码常量 |
| L2 length_ratio 折扣系数 | 0.7 | `router.py:178` | 硬编码常量 |
| L2 top_k | 1 | `router.py:709` | 硬编码 |
| L3 DAG 最低 confidence | 0.5 | `orchestrator.py:1077-1081` | 硬编码 |
| L3 ReAct 最低 confidence | (无) | — | 不存在分档 |

**结论**：所有路由阈值均无配置项，调优唯一手段是改源码 + 重启，无灰度能力。

### 2.6 L3 兜底策略

代码位置：[orchestrator.py:1077-1081](../core/orchestrator/orchestrator.py)

```python
low_confidence = (
    signal.intent_kind == _IntentKindRoute.ANALYSIS
    and signal.route_level == 3
    and signal.confidence < 0.5  # 硬编码
)
```

**结论**：单一阈值的二分判断，无中间档。

### 2.7 其他相关现状

- **intent_seeds.yaml** 已包含 12 个类目（10 analysis + data_lookup + meta），共约 145 条种子（[intent_seeds.yaml](../config/intent_seeds.yaml)）
- **L1 lookup 兜底**已在 intent_kind 改造时加入（`_looks_like_data_lookup`）
- **bypass 已按 IntentKind 拆分**（CHITCHAT / META / RECALL）
- 现有路由层测试 167 条（`tests/unit/test_router.py`），全部通过

---

## 3. 整体方案与执行顺序

### 3.1 5 批分阶段策略

| 批次 | 主题 | 核心动作 | 预估 ReAct 触发率变化 | 风险 | 工作量（行）|
|---|---|---|---|---|---|
| **批 1** | 阈值配置化 + 命中率监控 | 把所有硬编码阈值挪到 Settings；trace span 加 execution；新增 SQL/API | 0pp（行为等价） | 极低 | ~400 |
| **批 2** | L1 词库扩展 | 10 条规则补 5-10 个口语化同义词 | -8~15pp | 极低（只加不删）| ~200（数据为主）|
| **批 3** | L2 top-k 投票 | top-1→top-3 加权投票；length_ratio 折扣弱化 | -5~10pp | 低（默认 topk=1 等价）| ~250 |
| **批 4** | case_store 闭环 + L2.5 检索 | DAGExecutor 写入；新增 search API；L2 后 L3 前注入 L2.5 | -10~20pp（需案例积累）| 中（涉及多组件）| ~700 |
| **批 5** | 通用 DAG 模板 | 新增 RECENT_PROCUREMENT_HEALTH 模板；触发条件接入 | -5~10pp（直接吃概览流量）| 中（模板正确性需验证）| ~500 |

**累计预估**：ReAct 触发率从基线下降 **30~45 pp**（保守档），不破坏现有覆盖率。

### 3.2 推荐执行顺序

```
批 1 → 批 2 → 批 3 → 批 5 → 批 4
```

**顺序依据**：
1. **批 1 必须最先**——监控就位才能采基线、配置化才能灰度
2. **批 2 / 批 3 排次**——独立、低风险、立即见效，先跑两周收集数据
3. **批 5 优先于批 4**——批 5 自包含、效果立竿见影；批 4 需要案例库积累，先把 DAGExecutor 写入闭环跑起来积累数据
4. **批 4 最后**——依赖批 1（监控）+ 批 5（写入路径已稳定），且 L2.5 命中率取决于案例积累量，需要时间窗口

### 3.3 ROI 与风险矩阵

```
高 ROI │ 批 1 (基础设施)    批 2 (L1 词库)
       │
       │ 批 5 (通用模板)    批 3 (L2 top-k)
       │
       │ 批 4 (case_store)
低 ROI │
       └─────────────────────────────────────
         低风险              高风险
```

- **高 ROI + 低风险**：批 1、批 2 → 立即做
- **中等 ROI + 中风险**：批 3、批 5 → 第二阶段
- **高 ROI 但需要时间发酵**：批 4 → 最后做，依赖前置批次

### 3.4 灰度与切换策略

每批默认配置都等价当前硬编码行为。上线后按以下顺序切换：

| 步骤 | 时机 | 动作 |
|---|---|---|
| 1 | 批 1 上线 + 监控就位 | 采集 7 天基线（DAG 触发率 / L1L2L3 分布 / ReAct 触发率） |
| 2 | 批 2 上线 | 关注 L1 命中率提升 |
| 3 | 批 3 上线（topk 仍 = 1）| 验证无回归 |
| 4 | 切换 `l2_topk=3` | 观察 7 天 |
| 5 | 切换 `l2_similarity=0.72`（激进档）| 观察 |
| 6 | 切换 `l3_dag_min_confidence=0.4`（中档）| 观察 |
| 7 | 批 5 上线 + 启用通用模板 | 关注 COMPREHENSIVE 流量分流 |
| 8 | 批 4 上线（写入开启，L2.5 仍关闭）| 让案例库积累 1-2 周 |
| 9 | 启用 L2.5 检索（`l25_enabled=true`）| 观察案例命中率 |

**回滚原则**：任一档上线后 7 天内
- ReAct 触发率回升超过 5pp
- DAG 失败率上升超过 3pp
- P50 路由延迟上升超过 100ms

→ 立即配置回退（无需重新部署）。

### 3.5 不影响范围说明

本方案不会动以下既有机制（避免范围蔓延）：

| 现有机制 | 保留原因 |
|---|---|
| `intent_kind` 二段分类（CHITCHAT/META/CLARIFICATION/...） | 已稳定，本方案在 ANALYSIS / DATA_LOOKUP 内部优化 |
| bypass 拆分（CHITCHAT/META/RECALL 直出 sentinel）| 已正确，零改动 |
| `_render_intent_kind_template`（早退模板渲染）| 已稳定 |
| 现有 10 个 AnalysisType DAG 模板内容 | 不动模板内部，只动模板触发条件与新增模板 |
| 长期记忆 / 短期记忆链路 | 独立专题 |
| ReAct Agent prompt / tools | 独立专题 |

---

## 4. 批次 1：阈值配置化 + 命中率监控

### 4.1 目标

- 把 L1 / L2 / L3 所有路由阈值挪到 Pydantic Settings + config.yaml，**默认值等价当前硬编码**
- 给 trace span 补 `execution=dag|react` 属性，便于聚合
- 提供命中率 dashboard SQL + 只读 API
- 不改变任何路由行为，仅打基础设施

### 4.2 原子步骤

#### 步骤 1.1 · 新增 `IntentRoutingSettings` 子模型

**文件**：[config/settings.py](../config/settings.py)

新增 Pydantic 子模型，位置紧邻 `AnalysisSettings`：

| 字段 | 类型 | 默认值 | 保守档 | 激进档 | 含义 |
|---|---|---|---|---|---|
| `l1_threshold_default` | float | 0.15 | 0.15 | 0.12 | L1 大多数规则的命中率门槛 |
| `l1_threshold_strict` | float | 0.20 | 0.20 | 0.17 | INVOICE_DUPLICATE / VENDOR_CONCENTRATION 等高假阳性规则 |
| `l2_similarity_threshold` | float | 0.80 | 0.80 | 0.72 | L2 命中所需的最低相似度 |
| `l2_length_ratio_floor` | float | 0.4 | 0.4 | 0.4 | query/seed 长度比低于此值才打折 |
| `l2_length_ratio_penalty` | float | 0.7 | 0.7 | 0.85 | 长度比过低时的相似度乘子 |
| `l2_topk` | int | 1 | 1 | 3 | L2 检索返回的 top-k 数量 |
| `l3_dag_min_confidence` | float | 0.5 | 0.5 | 0.4 | L3 ANALYSIS 走 DAG 的最低置信度 |
| `l3_react_min_confidence` | float | 0.3 | 0.3 | 0.3 | L3 ANALYSIS 低于此值才走 ReAct（中间档预留批 4 用）|
| `l25_enabled` | bool | False | False | True | L2.5 案例检索 feature flag（批 4 启用）|
| `generic_template_enabled` | bool | True | True | True | 通用 DAG 模板 feature flag（批 5 启用）|

约束：所有字段必须有 `Field(default=..., description="...")`，描述要说清楚生效条件与单位。

#### 步骤 1.2 · 在 `config.yaml` 注册

**文件**：[config/config.yaml](../config/config.yaml)

新增段：

```yaml
intent_routing:
  # L1 关键词命中率门槛
  l1_threshold_default: 0.15
  l1_threshold_strict: 0.20
  # L2 Chroma 语义匹配
  l2_similarity_threshold: 0.80
  l2_length_ratio_floor: 0.4
  l2_length_ratio_penalty: 0.7
  l2_topk: 1
  # L3 LLM 分类置信度分档
  l3_dag_min_confidence: 0.5
  l3_react_min_confidence: 0.3
  # 后续批次的 feature flag
  l25_enabled: false
  generic_template_enabled: true
```

约束：所有默认值必须与 `Settings` 默认值一字不差，便于 [tests/unit/test_config.py](../tests/unit/test_config.py) 验证一致性。

#### 步骤 1.3 · `_RULE_LIBRARY` threshold 改为 settings-driven

**文件**：[core/orchestrator/router.py](../core/orchestrator/router.py)

- `_RULE_LIBRARY` 各 dict 的 `threshold` 字段语义改为"是否使用 strict 阈值"——保留 `0.15` / `0.20` 仅作为 fallback 标记，运行时由 `IntentRouter` 注入 settings 决定实际值
- `_try_level1` 内部用 `self._settings.intent_routing.l1_threshold_default / l1_threshold_strict` 取代硬编码
- 加日志：路由初始化时打印一次实际生效的阈值（INFO 级）

#### 步骤 1.4 · L2 阈值改为 settings-driven

**文件**：[core/orchestrator/router.py](../core/orchestrator/router.py)

- 删除模块常量 `_L2_LENGTH_RATIO_THRESHOLD` / `_L2_LENGTH_RATIO_DISCOUNT`
- L2 内 `0.80` similarity 阈值改为 `self._settings.intent_routing.l2_similarity_threshold`
- length_ratio 折扣改为 settings 注入
- `top_k` 参数改为 settings 注入（本批默认 1，批 3 切换）

#### 步骤 1.5 · L3 confidence 阈值改为 settings-driven

**文件**：[core/orchestrator/orchestrator.py](../core/orchestrator/orchestrator.py)

- `low_confidence` 计算中的 `0.5` 改为 `self._settings.intent_routing.l3_dag_min_confidence`
- 增加 trace span 属性 `route.l3_threshold_used`（便于回溯当时的配置）

#### 步骤 1.6 · trace span 补 `execution` 属性

**文件**：[core/orchestrator/orchestrator.py](../core/orchestrator/orchestrator.py)

在 `use_dag` 决策完成后，向 `intent.route_decision` 父 span 写入 `execution="dag"` 或 `"react"`，便于后续 SQL 直接 GROUP BY 聚合（避免间接推断）。

实现位置：`use_dag` 计算完成后、`_publish_stage_safe("dag_planned"/"react_started")` 调用之前。

#### 步骤 1.7 · 命中率监控 SQL + API

**新文件 1**：`scripts/route_hit_rate.sql`

输出字段：`analysis_type`、`intent_kind`、`route_level`、`execution`、`count`、`avg_confidence`、`p50_duration_ms`，按 7 天窗口聚合。

**新文件 2**：[api/routes/admin_metrics.py](../api/routes/admin_metrics.py)

```
GET /admin/metrics/route-hit-rate?window=7d
```

返回 JSON 数组，与 SQL 输出字段对应。鉴权遵循现有 `api/routes/` 约定（如已有 admin guard 则复用）。

注册到 [api/main.py](../api/main.py) 的 router include 列表。

#### 步骤 1.8 · 单测覆盖

**新增**：`tests/unit/test_intent_routing_settings.py`

| 测试用例 | 期望 |
|---|---|
| `test_default_values_match_hardcoded` | Settings 默认值 == 当前 router.py / orchestrator.py 硬编码值 |
| `test_yaml_overrides_settings` | config.yaml 修改后 Settings 反映 |
| `test_router_uses_injected_thresholds` | 修改 settings.intent_routing.l1_threshold_default 后，L1 行为变化 |
| `test_l2_topk_respected` | settings.l2_topk=3 时 Chroma `search` 被调用 with `n_results=3` |
| `test_l3_threshold_injected` | settings.l3_dag_min_confidence=0.4 时，confidence=0.45 走 DAG |
| `test_execution_attribute_in_span` | `intent.route_decision` span 含 `execution` 属性 |
| `test_admin_metrics_route_hit_rate` | GET API 返回结构正确，window 参数支持 1d/7d/30d |

**回归**：现有 `tests/unit/test_router.py` 167 条 + `test_orchestrator.py` 全部应继续通过（默认配置等价）。

### 4.3 验收标准

- [ ] `Settings.intent_routing.*` 全部字段有默认值且 yaml 可覆盖
- [ ] router.py / orchestrator.py 内不再出现 `0.15` / `0.20` / `0.80` / `0.7` / `0.4` / `0.5` 这些硬编码数字（grep 校验）
- [ ] `intent.route_decision` span 含 `execution` 字段
- [ ] `scripts/route_hit_rate.sql` 在测试库可执行返回结果
- [ ] `GET /admin/metrics/route-hit-rate` 返回非空 JSON
- [ ] 全套测试 ≥ 90% 覆盖率，新增至少 7 个测试用例
- [ ] 启动日志包含一行 "intent_routing settings: l1=... l2=... l3=..."

### 4.4 回滚策略

本批为基础设施改动，无行为变更——若发现 settings 读取错误，可：
1. yaml 删 `intent_routing` 段 → fallback 到 Settings 默认值
2. 极端情况：`git revert` 单 commit 即可，不影响其他批次

---

## 5. 批次 2：L1 词库扩展

### 5.1 目标

- 给 [router.py `_RULE_LIBRARY`](../core/orchestrator/router.py) 10 条规则各补 5-10 个口语化同义词
- 不调阈值，仅靠词频提升直接提高 L1 命中率
- 新增同义词都要有单测覆盖

### 5.2 原子步骤

#### 步骤 2.1 · 词库扩展（10 条规则同义词补充清单）

**文件**：[core/orchestrator/router.py](../core/orchestrator/router.py) `_RULE_LIBRARY`

下表给出每条规则建议补充的同义词。原则：
- **覆盖口语化表达**（用户实际怎么说，不是教科书术语）
- **覆盖动作 + 对象 + 痛点 三个维度**
- **避免与其他规则的关键词重叠**（重叠会降低单一规则的命中率优势）

| AnalysisType | 现有关键词（节选） | 建议补充 |
|---|---|---|
| `THREE_WAY_MATCH` | 三路匹配, 三单, 匹配, three way, 发票, 收货 | `三单核对`, `三方对账`, `单据不一致`, `数量不符`, `金额不符`, `gr 和 invoice`, `对不上`, `匹配失败` |
| `PRICE_VARIANCE` | 价格差异, 价格, price, variance, ppv, 标准价, 合同价 | `涨价`, `贵了多少`, `比上次贵`, `单价波动`, `砍价空间`, `溢价`, `价差分析`, `价格异常`, `成本偏高` |
| `PAYMENT_COMPLIANCE` | 付款, 逾期, payment, overdue, 到期, 应付, 账期 | `拖欠`, `没付`, `欠款`, `欠多久`, `账期超`, `延迟付款`, `付款违规`, `账期合规`, `应付未付`, `付款超期` |
| `SUPPLIER_PERFORMANCE` | 供应商, 绩效, kpi, supplier, performance, 准时交货, otif | `交期表现`, `供应商评分`, `供货质量`, `合作表现`, `供应商打分`, `交付率`, `表现差`, `供应商等级` |
| `SPEND_ANALYSIS` | 支出, spend, 采购金额, 花费, 费用, 品类 | `开销`, `用了多少钱`, `花了多少`, `采购总额`, `预算消耗`, `品类支出`, `支出占比`, `花销分布` |
| `RECEIPT_ANOMALY` | 收货, 超量, 拒收, 退货, 延迟收货, receipt | `多收`, `少收`, `收货异常`, `退货率`, `质量问题`, `验收不合格`, `验货失败`, `收货拒收` |
| `INVOICE_DUPLICATE` | 重复发票, duplicate, 重复, 相同发票, 重复开票, 重复付款 | `双开`, `一票两付`, `重号`, `同号发票`, `重复入账`, `多付`, `付两次`, `开重了` |
| `DISCOUNT_UTILIZATION` | 折扣, discount, 早付, 提前付款折扣, 折扣利用 | `没拿到折扣`, `错过折扣`, `折扣损失`, `折扣利用率低`, `节省了多少`, `应得折扣`, `折扣覆盖率` |
| `PO_CYCLE_TIME` | 周期, cycle, 耗时, 时效, lead time | `处理时间`, `处理慢`, `审批太久`, `效率低`, `从下单到`, `多久能到`, `处理快慢`, `lead time 分析` |
| `VENDOR_CONCENTRATION` | 集中度, 依赖, concentration, 单一来源, 占比, 垄断 | `太集中`, `依赖度`, `供应商太少`, `单一供应商`, `多元化不足`, `谁供得多`, `供应商分布`, `供应集中` |

**实施约束**：
- 每条规则补完后总关键词数控制在 15-25 之间（过多会让命中率分母变大反而拉低 hit_rate，得不偿失）
- 不引入数字、英文标点（避免与正则参数提取冲突）
- 不引入"分析 / 检查 / 查看"这类通用动词（已在 `_ANALYSIS_KEYWORDS` 集合内单独使用）

#### 步骤 2.2 · 单测覆盖

**新增文件**：`tests/unit/test_router_l1_synonyms.py`

| 测试场景 | 测试用例数 |
|---|---|
| 每条规则的新增同义词，至少 1 个口语 query 命中预期类型 | ~80（每规则 8 条同义词 × 10 规则）|
| 新关键词不应误命中其他规则 | 10（每规则一条对照查询）|
| 总关键词数边界（15-25）| 10 |

**测试模板**（pseudo）：
```python
@pytest.mark.parametrize("query,expected_type", [
    ("供应商一直在拖欠付款", AnalysisType.PAYMENT_COMPLIANCE),
    ("最近几单价格涨了不少", AnalysisType.PRICE_VARIANCE),
    ("有没有重复开票的情况", AnalysisType.INVOICE_DUPLICATE),
    # ...
])
def test_l1_colloquial_query_hits_expected_type(query, expected_type):
    router = IntentRouter(settings=Settings())
    signal = router._try_level1(query, {})
    assert signal is not None
    assert signal.keywords == [expected_type.value]
```

**回归**：现有 `tests/unit/test_router.py::TestLevel1` 全部应继续通过（只加不删）。

### 5.3 验收标准

- [ ] 10 条规则全部补充同义词，每条总关键词数 ∈ [15, 25]
- [ ] 新增至少 80 条口语化 query 测试，全部命中预期 analysis_type
- [ ] 现有 167 条 router 测试零回归
- [ ] L1 命中率（基于批 1 监控）观察 7 天提升 ≥ 5 pp（保守目标）

### 5.4 回滚策略

纯数据改动，无行为风险。若某条规则误命中率上升：
1. 在 `_RULE_LIBRARY` 移除该规则的某些同义词即可
2. 极端：单 commit revert

---

## 6. 批次 3：L2 top-k 投票

### 6.1 目标

- 把 L2 检索从 top-1 改为 top-k 加权投票（默认 k=3），降低单条种子噪声的影响
- 弱化 length_ratio 折扣，避免短查询被过度惩罚
- topk=1 + 默认折扣 == 完全等价当前行为，灰度切换

### 6.2 原子步骤

#### 步骤 3.1 · L2 search 改为 top-k

**文件**：[core/orchestrator/router.py](../core/orchestrator/router.py) `_try_level2`

- Chroma `search(query=query, top_k=self._settings.intent_routing.l2_topk)`
- 默认 `l2_topk=1`（沿用），灰度切 3
- topk=1 时直接走原有单条命中逻辑（避免不必要的聚合开销）

#### 步骤 3.2 · 加权投票聚合算法

**文件**：[core/orchestrator/router.py](../core/orchestrator/router.py)

新增私有方法 `_aggregate_l2_votes(results)`：

```
输入：list[dict]（Chroma 返回的 top-k 结果，每条含 metadata.analysis_type, distance）

算法：
  1. 计算每条 similarity = 1 - distance
  2. 按 rank 加权：rank_weights = [1.0, 0.6, 0.3, 0.15, ...]（指数衰减）
  3. 按 metadata.analysis_type 分桶
  4. 每桶分数 = Σ(similarity_i × rank_weight_i)
  5. 取最高桶；若桶内 top1 sim ≥ similarity_threshold 或 桶分数 ≥ similarity_threshold × 1.2 → 命中
  6. 否则返回 None（让 L3 处理）

输出：(analysis_type | None, aggregated_confidence | None, reasoning_text)
```

边界处理：
- 空结果 → None
- 同 analysis_type 出现多次 → 累加分数（避免单类型被稀释）
- data_lookup / meta sentinel 类目按现有逻辑独立判断（不参与投票）

#### 步骤 3.3 · length_ratio 折扣弱化

**文件**：[core/orchestrator/router.py](../core/orchestrator/router.py)

- 默认 penalty 0.7 → 0.85（更宽松，由 settings 配置）
- **仅当 top-1 单独命中时打折**（top-k 投票场景下，多种子已经能消化噪声，无需打折）
- 投票场景下 length_ratio 仅作为 trace 字段记录，不影响分数

#### 步骤 3.4 · 单测覆盖

**新增**：`tests/unit/test_router_l2_voting.py`

| 测试场景 | 期望 |
|---|---|
| `test_topk_1_equivalent_to_original` | settings.l2_topk=1 时行为等价旧版本（snapshot 测）|
| `test_topk_3_voting_correct_winner` | top-1 是噪声、top-2/3 一致 → 投票胜出 top-2/3 的类型 |
| `test_topk_3_high_top1_wins` | top-1 sim=0.95、top-2/3 sim=0.5 → top-1 胜出 |
| `test_topk_3_tied_categories_aggregate` | 同 analysis_type 出现 2 次 → 分数累加 |
| `test_topk_3_below_threshold_returns_none` | 所有桶分数都 < 阈值 → 返回 None 让 L3 处理 |
| `test_length_ratio_penalty_only_top1` | topk=3 时 length_ratio<0.4 不打折；topk=1 时仍打折 |
| `test_data_lookup_sentinel_in_topk` | metadata.analysis_type=='data_lookup' 仍按 sentinel 单独处理 |
| `test_meta_sentinel_in_topk` | metadata.analysis_type=='meta' 同上 |

**回归**：现有 `tests/unit/test_router.py::TestLevel2`、`TestL2SentinelCategoryMatching` 全部应通过（默认 topk=1 等价）。

### 6.3 验收标准

- [ ] `_try_level2` 在 topk=1 时行为完全等价旧版本
- [ ] `_aggregate_l2_votes` 算法独立可测
- [ ] 8 个新测试用例全部通过
- [ ] 现有 L2 相关测试零回归
- [ ] 切换 topk=3 后 L2 命中率（基于批 1 监控）观察 7 天提升 ≥ 3 pp

### 6.4 回滚策略

- 单一配置回退：`l2_topk: 1` 即可恢复旧行为
- 投票算法 bug 紧急情况：注释 `_try_level2` 内对 `_aggregate_l2_votes` 的调用，强制走 top-1 路径
- 极端：commit revert

---

## 7. 批次 4：case_store 写入闭环 + L2.5 检索

### 7.1 目标

- 把 `case_store.store_successful_case` 接入 DAGExecutor 成功路径，让案例库真正积累数据
- 给 `case_store` 新增 `search_similar_case()` 检索 API
- 在路由链 L2 后、L3 前注入 **L2.5 案例检索层**：命中已有成功案例时直接复用其 `dag_definition`，跳过 L3 + 模板加载
- 全程由 `intent_routing.l25_enabled` feature flag 控制（默认 False，等案例积累后开启）

### 7.2 原子步骤

#### 步骤 4.1 · 新增 `search_similar_case` API

**文件**：[core/orchestrator/dag/case_store.py](../core/orchestrator/dag/case_store.py)

新增方法：

```python
def search_similar_case(
    self,
    query: str,
    *,
    top_k: int = 3,
    min_similarity: float = 0.75,
) -> dict | None:
    """检索与 query 语义相似的成功案例。

    内部用 self._chroma_store.search 复用已有 Chroma 集合
    （由 load_to_chroma_on_startup 启动时填充 + store_successful_case 增量写入）。

    返回结构：
    {
      "query": <case query>,
      "analysis_type": <str>,
      "dag_definition": <list[dict]>,  # 可直接喂给 DAGExecutor
      "similarity": <float>,
      "duration_sec": <float>,         # 历史执行时长，用于估时
      "task_count": <int>,
    }
    或 None（无命中）
    """
```

实现要点：
- 复用现有 Chroma 集合（不新建）
- top-1 距离 < `1 - min_similarity` 视为命中
- metadata 中的 `dag_definition` 是 JSON 字符串，需 `json.loads` 还原
- 失败（Chroma down / json 解析失败）→ 返回 None + WARNING 日志，不阻断

#### 步骤 4.2 · DAGExecutor 接入写入

**文件**：[core/orchestrator/dag/executor.py](../core/orchestrator/dag/executor.py)

在 `execute()` 方法的成功路径末尾（status=="ok" 且无失败任务时）调用：

```python
await self._case_store.store_successful_case(
    query=...,
    analysis_type=...,
    dag=dag_tasks,
    route_type="DAG",
    exec_result=result,
)
```

实现要点：
- `case_store` 实例通过依赖注入（DAGExecutor `__init__` 接收 `case_store: CaseStore | None = None`）
- 异步、失败不阻断主流程（`store_successful_case` 内部已有 try/except）
- 加 trace span `case_store.write` 记录写入耗时与结果
- 可由 `intent_routing.l25_enabled` 控制是否写入（默认 False 时也写入，保证案例库正常积累；只是检索层不开启）

**重要**：写入是无副作用的（仅累积数据），所以默认开启写入；只有"消费写入"的 L2.5 检索由 flag 控制。

#### 步骤 4.3 · router 新增 `_try_level25` 方法

**文件**：[core/orchestrator/router.py](../core/orchestrator/router.py)

新增方法 `_try_level25(query, params) -> QuerySignal | None`：

```
逻辑：
  1. 若 settings.intent_routing.l25_enabled == False → 直接 return None
  2. 调 self._case_store.search_similar_case(query, top_k=3, min_similarity=...)
  3. 命中 → 构造 QuerySignal:
     - intent_kind = ANALYSIS
     - keywords = [analysis_type]
     - entities = params  + ["__dag_definition__": dag_def]  ← 关键：把 DAG 直接传出
     - route_level = 25  ← 新增层级标识
     - confidence = similarity
     - reasoning = "L2.5 案例命中：'<原 query>...' 相似度 X%"
  4. 未命中或 Chroma down → return None
```

#### 步骤 4.4 · 路由链注入 L2.5

**文件**：[core/orchestrator/router.py](../core/orchestrator/router.py) `_route` 方法

调用顺序变更：

```
原顺序：bypass → L1 → L2 → L3
新顺序：bypass → L1 → L2 → L2.5 → L3
```

实现要点：
- L2.5 仅在 L2 未命中时尝试
- L2.5 也未命中才进 L3
- trace span 增加 `intent.l25` 子 span 与 `hit_level=25`

#### 步骤 4.5 · orchestrator 复用 L2.5 命中的 DAG

**文件**：[core/orchestrator/orchestrator.py](../core/orchestrator/orchestrator.py)

DAG 决策块识别 `signal.route_level == 25`：
- 若 `signal.entities` 含 `__dag_definition__` → 跳过 `load_dag_template`，直接喂 `dag_executor.execute(dag_definition_from_case)`
- trace span 标记 `route.l25_reused=true`，便于统计 L2.5 复用率

**安全护栏**：复用前用 `DAGValidator` 校验 dag_definition；校验失败 → 降级走 L3 + 模板加载，不让坏案例传播。

#### 步骤 4.6 · 单测覆盖

**新增文件 1**：`tests/unit/test_case_store_search.py`

| 测试场景 |
|---|
| `search_similar_case` 返回 None when Chroma 空 |
| `search_similar_case` 命中时返回正确结构（含 dag_definition）|
| `search_similar_case` 距离过远 → None |
| `search_similar_case` Chroma 异常 → None + WARNING 日志 |
| `search_similar_case` metadata.dag_definition 为非 JSON → None |

**新增文件 2**：`tests/unit/test_router_l25.py`

| 测试场景 |
|---|
| `_try_level25` flag 关闭时直接返回 None |
| `_try_level25` 命中时构造正确 signal（route_level=25, entities 含 __dag_definition__）|
| `_try_level25` 不影响 L1/L2 命中（仅在 L2 失败后调用）|
| 路由链顺序：bypass → L1 → L2 → L2.5 → L3 |
| trace span `intent.l25` 出现且含正确属性 |

**新增文件 3**：`tests/unit/test_executor_case_store_write.py`

| 测试场景 |
|---|
| DAGExecutor 成功执行后调 `store_successful_case` 一次 |
| 失败任务时不调 `store_successful_case` |
| `case_store` 写入失败时主流程仍返回成功 |
| 未注入 `case_store` 时不报错（None 兼容）|

**新增文件 4**：`tests/integration/test_l25_end_to_end.py`

| 测试场景 |
|---|
| 端到端：先写入一个案例 → 类似 query 再来 → L2.5 命中 → 复用 DAG → 成功输出 |
| flag 关闭时同样 query 走原 L3 流程 |

**回归**：现有 router/orchestrator 测试零回归（默认 flag=False，行为等价）。

### 7.3 验收标准

- [ ] `case_store.search_similar_case` 实现 + 单测通过
- [ ] DAGExecutor 写入路径上线后，案例库 PG 表行数随时间增长（监控）
- [ ] L2.5 默认关闭，开启后端到端测试通过
- [ ] `signal.route_level=25` 在 trace 中可查询
- [ ] 全套测试 ≥ 90% 覆盖率
- [ ] 启用 L2.5 后观察 14 天，命中率 ≥ 5%（取决于流量重复度）

### 7.4 冷启动策略

L2.5 启用时若案例库为空：
- `search_similar_case` 直接返回 None
- 路由链自然 fallback 到 L3
- 服务运行一段时间后案例自动积累，命中率逐步提升

**建议时间线**：
1. 批 4 上线，DAGExecutor 写入开启，`l25_enabled=False`
2. 观察 1-2 周，案例库 ≥ 100 条且分布合理
3. 切 `l25_enabled=true` 灰度（先内部用户）
4. 观察 7 天命中率与回归
5. 全量开启

### 7.5 回滚策略

| 故障 | 回滚动作 |
|---|---|
| L2.5 误命中导致错误 DAG 复用 | `l25_enabled=false` 即停 |
| `search_similar_case` 性能问题（>50 ms） | 同上 |
| `case_store.store_successful_case` 写入异常导致 DAGExecutor 卡住 | 已有 try/except 兜底；最坏在 executor 接入处加 if-flag 关闭写入 |
| Chroma collection 数据污染 | 清空 Chroma intent_cases collection（PG 数据不动）+ 重启 → `load_to_chroma_on_startup` 重新加载 |

---

## 8. 批次 5：通用 DAG 模板 RECENT_PROCUREMENT_HEALTH

### 8.1 目标

- 收编"COMPREHENSIVE 无实体"的概览类查询（"看看最近采购"/"近期采购总体怎么样"）
- 新增 1 个通用 DAG 模板 `RECENT_PROCUREMENT_HEALTH`，提供标准化的"近期采购健康度"答案
- 由 `intent_routing.generic_template_enabled` feature flag 控制，便于回滚

### 8.2 模板设计

#### 模板名称
`RECENT_PROCUREMENT_HEALTH`（近期采购健康度概览）

#### 任务编排（4 节点 DAG）

```
T1: 近 N 天 PO 总量 / 总额统计
    └─ tool: query_purchase_orders（聚合 count + sum）

T2: 异常 PO TopN（金额 / 延期）
    └─ tool: rule_three_way_match + rule_payment_compliance
    └─ 取 top 5 高严重度异常

T3: 供应商表现 TopN
    └─ tool: rule_supplier_performance（KPI 计算）
    └─ 取 top 5 准时率最低 + top 5 金额最大

T4: 汇总报告
    └─ tool: report_agent
    └─ 输入：T1/T2/T3 输出
    └─ 模板化 prompt：标题"近 N 天采购健康度概览" + 4 段（总量/异常/供应商/建议）
```

#### 触发条件

`intent_kind == ANALYSIS && analysis_type == COMPREHENSIVE && 无实体 && query 含触发词`

触发词集合（任一命中即可）：
```
{ "最近", "近期", "最新", "概览", "总体", "总览", "整体", "全面",
  "现状", "情况", "怎么样", "采购情况", "近况" }
```

### 8.3 原子步骤

#### 步骤 5.1 · 模板定义

**文件**：[core/orchestrator/dag/templates.py](../core/orchestrator/dag/templates.py)

新增模板定义函数 `_recent_procurement_health_template(params)`：
- 接收 `params: dict`（含 `days`，默认从 settings 取）
- 返回 list[dict] 形式的 DAG 任务列表（4 节点，结构与现有模板一致）
- 任务的 `task_id` 用 `t1_po_summary` / `t2_anomaly_topn` / `t3_supplier_topn` / `t4_report` 命名

注册到 `load_dag_template` 的派发逻辑（新增独立分支，不与现有 AnalysisType 模板冲突）。

**实现约束**：
- 全部用现有 P2P tools（不新增 tool）
- T1/T2/T3 并行（depends_on 都为空）
- T4 依赖 T1/T2/T3 全部完成

#### 步骤 5.2 · orchestrator 路由决策接入

**文件**：[core/orchestrator/orchestrator.py](../core/orchestrator/orchestrator.py)

在 `use_dag` 决策的"COMPREHENSIVE + 无实体"分支前插入新分支：

```python
if (
    settings.intent_routing.generic_template_enabled
    and signal.intent_kind == IntentKind.ANALYSIS
    and analysis_type == AnalysisType.COMPREHENSIVE
    and not has_entity
    and _query_matches_overview_keywords(request.query)
):
    use_dag = True
    dag_template_key = "recent_procurement_health"  # 传给 _execute_dag
```

新增辅助函数 `_query_matches_overview_keywords(query: str) -> bool`：
- 触发词集合定义在 orchestrator.py 模块级（参考 8.2 节）
- 子串匹配（中文无分词）

`_execute_dag` 接收 `dag_template_key` 参数，传给 `load_dag_template(dag_template_key, params)`。

#### 步骤 5.3 · feature flag 控制

**文件**：[config/settings.py](../config/settings.py) + [config/config.yaml](../config/config.yaml)

`intent_routing.generic_template_enabled`（已在批 1 占位）：
- 默认 True（直接启用）
- 关闭时降级到 ReAct（现有行为）

#### 步骤 5.4 · 单测覆盖

**新增文件**：`tests/unit/test_dag_recent_procurement_health.py`

| 测试场景 |
|---|
| 模板返回 4 节点 DAG，task_id 与 depends_on 正确 |
| `_query_matches_overview_keywords("看看最近采购情况")` → True |
| `_query_matches_overview_keywords("查询 SUP-001 的发票")` → False |
| 触发条件全满足时 use_dag=True 且 template_key 正确 |
| flag 关闭时不触发，降级 ReAct |
| 含实体的 query 不触发（应走原 COMPREHENSIVE+entity 分支）|
| 不含触发词的 COMPREHENSIVE 不触发（保持原 ReAct 兜底）|

**新增集成测试**：`tests/integration/test_recent_procurement_health.py`

| 场景 |
|---|
| 端到端：query="看看最近采购情况" → DAG 执行 → 4 节点全部成功 → 报告输出含 4 段结构 |
| 部分节点失败时 status=partial_success，报告仍生成 |

**回归**：现有 orchestrator 测试零回归（默认 flag=True 时新分支被覆盖；新分支只在特定 query 触发，不影响其他流量）。

### 8.4 验收标准

- [ ] `_recent_procurement_health_template` 实现 + 单测通过
- [ ] orchestrator 路由决策正确分流（含触发词的 COMPREHENSIVE 无实体 → DAG；其他不变）
- [ ] 端到端测试通过（4 节点 DAG 输出有意义的报告）
- [ ] 全套测试 ≥ 90% 覆盖率
- [ ] 上线后观察 7 天，"COMPREHENSIVE+无实体" 流量中 ≥ 30% 走入新模板

### 8.5 回滚策略

- `generic_template_enabled: false` → 立即恢复 ReAct 兜底
- 模板内某节点 bug → 在模板定义中临时移除该节点（重新部署）
- 极端：commit revert，不影响其他批次

### 8.6 后续扩展空间

本批次只新增 1 个模板（`RECENT_PROCUREMENT_HEALTH`）。后续可考虑：
- `MULTI_DIM_OVERVIEW`（多维概览，含 PO/付款/收货 三大主表趋势）—— 需要产品定义"概览"标准
- `WEEKLY_SUMMARY`（周报模板）—— 需要业务流程对齐

这些都属于未来专题，本次不做。

---

## 9. 不做项与 punt 决策

明确列出本专题**不做**的事项，避免范围蔓延，同时记录 punt 理由便于未来再评估。

### 9.1 L3 confidence < 0.3 强行走 DAG

**为什么不做**：
- 低置信度意味着 LLM 自身不确定 analysis_type；强行走 DAG 模板可能产生"答非所问"的报告
- 用户感知最差的不是"ReAct 慢"，而是"DAG 给了错误的结构化答案"
- 当前批 4 的 L2.5 已经能消化大部分重复 query，不必牺牲准确性

**未来再评估的条件**：
- 批 1 监控显示 L3 confidence ∈ [0.2, 0.5) 区间内 LLM 给出的 analysis_type 实际上有 70% 以上正确率
- 那时可考虑加 `l3_marginal_dag_enabled` flag 单独灰度

### 9.2 L1.5 文本归一化缓存（Redis）

**原方案**：把 query 归一化（去标点 / 繁简 / 时间词占位化）后做 md5 缓存，命中直接复用上一次路由结果。

**为什么不做**：
- 与批 4 的 L2.5 案例检索功能高度重叠——案例库本身就是"基于历史成功 query 的查询缓存"
- 引入 Redis 一致性 / 失效 / TTL 复杂度，但实际收益（延迟 -100~300ms）不如解决 ReAct 触发率根因
- 归一化激进会误命中（"近 7 天" vs "近 30 天" 被合并），保守又会失去命中率

**未来再评估的条件**：
- 批 4 上线后 P50 路由延迟仍 > 500ms
- 监控发现高频重复 query（同 hash 出现 ≥ 100 次/天）
- 那时可考虑作为 L0.5 层（在 bypass 之后、L1 之前）

### 9.3 种子提案工作流（L3 输出 → intent_seeds.yaml）

**原方案**：L3 高置信度（>0.85）输出进入 `intent_seed_candidates` 表，定期人工 review 后批量合入 yaml。

**为什么不做**：
- intent_seeds.yaml 是 **schema 配置文件**（人工 curated），不应被运行时直接改写
- 已有批 4 的 case_store 走"自动写入 PG + Chroma"路径，不需要再走 yaml
- 涉及人工 review 流程，不是工程问题

**未来再评估的条件**：
- case_store 案例积累后，发现某些 analysis_type 的种子库覆盖明显不足
- 由产品/运营手动收集 query 样本补到 yaml 即可，不需要新表/新工作流

### 9.4 路由置信度模型化（朴素贝叶斯 / BERT 小模型）

**原方案**：训练一个轻量分类器作为 L1.5 或 L2.5。

**为什么不做**：
- **数据未就绪**：当前没有"被人工标注的 query → analysis_type"数据集；只有 case_store 的成功案例（正样本），无负样本
- 训练 / 推理都引入新依赖（torch / transformers / sklearn）
- 推理延迟（CPU 50-100ms）与现有 L2 Chroma 同量级，但 ROI 远不如直接做批 4

**未来再评估的条件**：
- 批 4 跑满半年，case_store 积累 ≥ 5000 条带标签数据
- 那时可考虑 **离线训练 + 在线检索**（不替换 L2/L3，而是作为补充信号）

### 9.5 阈值激进档（`l2_similarity_threshold=0.72` / `l3_dag_min_confidence=0.4`）默认开启

**为什么不做**：
- 默认开启会改变现有路由行为，违反"零破坏性"约束
- 必须先在批 1 监控就位后采集基线，再灰度切换
- 切换时机由批 3.4 的灰度顺序定义

**未来再评估的条件**：
- 灰度观察 14 天，DAG 失败率不上升、ReAct 触发率显著下降
- 那时可把 yaml 默认值切到激进档，并更新 Settings 默认

### 9.6 ReAct Agent prompt / tool 集优化

**为什么不在本专题**：
- 这是独立专题（已有 [docs/prompt_issue.md](prompt_issue.md) 部分覆盖）
- 本专题目标是"减少 ReAct 触发"，而不是"提升 ReAct 质量"
- 两者改进可并行推进，互不阻塞

### 9.7 路由层 metrics（Prometheus / OpenTelemetry counter）

**为什么不在本专题**：
- 当前 trace span 已经能聚合命中率（批 1 SQL/API 提供）
- Prometheus 接入需要新增 exporter 依赖，且涉及部署架构变更
- 短期内 SQL/API 足够支撑灰度决策

**未来再评估的条件**：
- 团队引入 Grafana / Prometheus 后，再统一接入 metrics

---

## 10. 验证方法、回滚策略与文件改动定位

### 10.1 端到端验证清单

#### 批 1（阈值配置化 + 监控）

```bash
# 配置等价性测试
pytest tests/unit/test_intent_routing_settings.py -v

# 全套回归
pytest --cov=. --cov-fail-under=90

# 启动校验：日志中应出现配置打印
grep "intent_routing settings:" logs/eragent.log

# SQL 校验
psql -f scripts/route_hit_rate.sql

# API 校验
curl -X GET 'http://localhost:8000/admin/metrics/route-hit-rate?window=7d' | jq
```

#### 批 2（L1 词库扩展）

```bash
# 新同义词测试
pytest tests/unit/test_router_l1_synonyms.py -v

# 现有 L1 测试零回归
pytest tests/unit/test_router.py::TestLevel1 -v

# 灰度观察（批 1 监控就位后）
curl '...?window=7d' | jq '.[] | select(.route_level=="1") | .count'
# 期望 7 天后该值上升 ≥ 5 pp
```

#### 批 3（L2 top-k 投票）

```bash
# 投票算法测试
pytest tests/unit/test_router_l2_voting.py -v

# 等价性测试（topk=1）
pytest tests/unit/test_router.py::TestLevel2 -v

# 切换到 topk=3 后端到端
yq -i '.intent_routing.l2_topk = 3' config/config.yaml
pytest tests/integration/test_e2e.py::test_l2_voting_path
```

#### 批 4（case_store 闭环 + L2.5）

```bash
# 单元测试
pytest tests/unit/test_case_store_search.py -v
pytest tests/unit/test_router_l25.py -v
pytest tests/unit/test_executor_case_store_write.py -v

# 集成测试
pytest tests/integration/test_l25_end_to_end.py -v

# 案例库积累验证（运行一段时间后）
psql -c "SELECT COUNT(*) FROM dag_cases;"
psql -c "SELECT analysis_type, COUNT(*) FROM dag_cases GROUP BY 1;"

# L2.5 命中率（启用 flag 后）
curl '...?window=7d' | jq '.[] | select(.route_level=="25") | .count'
```

#### 批 5（通用模板）

```bash
# 单测
pytest tests/unit/test_dag_recent_procurement_health.py -v

# 集成测试
pytest tests/integration/test_recent_procurement_health.py -v

# 端到端验证（API 调用）
curl -X POST 'http://localhost:8000/analyze' \
  -H 'Content-Type: application/json' \
  -d '{"query": "看看最近采购情况", "user_id": "test"}'
# 期望返回结构化报告（4 段），summary.route_type == "DAG"
```

### 10.2 灰度切换决策表

| 当前阶段 | 监控指标 | 切下一档条件 | 回滚条件 |
|---|---|---|---|
| 批 1 上线 | DAG 触发率 / L123 分布 | 采集 7 天基线，无异常 | 启动失败 / 配置读取错误 |
| 批 2 上线 | L1 命中率 | +5pp 后稳定 7 天 | 误命中率上升 |
| 批 3 上线（topk=1）| L2 命中率不变 | 等价性验证通过 | 任何 L2 测试回归 |
| `l2_topk=3` | L2 命中率 | +3pp 后稳定 7 天 | 命中率反降 / DAG 失败率 +3pp |
| `l2_similarity=0.72` | L2 命中率 | +5pp 累计 | 误命中率显著上升 |
| `l3_dag_min_confidence=0.4` | DAG 触发率 / 报告质量人工抽检 | DAG 报告质量评分不下降 | 报告评分下降 ≥ 5% |
| 批 5 上线 | "概览类" query 走 DAG 比例 | ≥ 30% | 模板执行失败率 ≥ 10% |
| 批 4 写入开启 | dag_cases 表行数增长 | 1 周积累 ≥ 100 条 | DAGExecutor 异常率上升 |
| `l25_enabled=true` | route_level=25 占比 | ≥ 5% 且报告正确 | L2.5 命中后报告错误率 ≥ 3% |

### 10.3 文件改动定位清单

| 批次 | 文件 | 改动类型 | 关键位置 |
|---|---|---|---|
| 1 | [config/settings.py](../config/settings.py) | 新增类 | `IntentRoutingSettings` Pydantic 子模型 |
| 1 | [config/config.yaml](../config/config.yaml) | 新增段 | `intent_routing:` |
| 1 | [core/orchestrator/router.py](../core/orchestrator/router.py) | 改造 | `_RULE_LIBRARY` threshold + `_try_level1` + `_try_level2` 内常量替换 |
| 1 | [core/orchestrator/orchestrator.py](../core/orchestrator/orchestrator.py) | 改造 | `low_confidence` 取 settings；`use_dag` 后 span 加 `execution` |
| 1 | scripts/route_hit_rate.sql | 新建 | 7 天命中率聚合 SQL |
| 1 | api/routes/admin_metrics.py | 新建 | `GET /admin/metrics/route-hit-rate` |
| 1 | [api/main.py](../api/main.py) | 改造 | router include |
| 1 | tests/unit/test_intent_routing_settings.py | 新建 | 7 个测试用例 |
| 2 | [core/orchestrator/router.py](../core/orchestrator/router.py) | 数据 | `_RULE_LIBRARY` 各规则 keywords 集合扩充 |
| 2 | tests/unit/test_router_l1_synonyms.py | 新建 | ~80 条口语化 query 参数化测试 |
| 3 | [core/orchestrator/router.py](../core/orchestrator/router.py) | 改造 | `_try_level2` + 新增 `_aggregate_l2_votes` |
| 3 | tests/unit/test_router_l2_voting.py | 新建 | 8 个测试用例 |
| 4 | [core/orchestrator/dag/case_store.py](../core/orchestrator/dag/case_store.py) | 新增方法 | `search_similar_case()` |
| 4 | [core/orchestrator/dag/executor.py](../core/orchestrator/dag/executor.py) | 改造 | 成功路径调 `store_successful_case` |
| 4 | [core/orchestrator/router.py](../core/orchestrator/router.py) | 新增方法 + 路由链 | `_try_level25`、`_route` 注入 L2.5 |
| 4 | [core/orchestrator/orchestrator.py](../core/orchestrator/orchestrator.py) | 改造 | DAG 决策识别 `route_level=25` 复用 dag_definition |
| 4 | tests/unit/test_case_store_search.py | 新建 | search 方法测试 |
| 4 | tests/unit/test_router_l25.py | 新建 | L2.5 路由测试 |
| 4 | tests/unit/test_executor_case_store_write.py | 新建 | 写入闭环测试 |
| 4 | tests/integration/test_l25_end_to_end.py | 新建 | 端到端测试 |
| 5 | [core/orchestrator/dag/templates.py](../core/orchestrator/dag/templates.py) | 新增模板 | `_recent_procurement_health_template` + 注册 |
| 5 | [core/orchestrator/orchestrator.py](../core/orchestrator/orchestrator.py) | 改造 | 路由决策新增分支 + `_query_matches_overview_keywords` |
| 5 | tests/unit/test_dag_recent_procurement_health.py | 新建 | 模板与触发条件测试 |
| 5 | tests/integration/test_recent_procurement_health.py | 新建 | 端到端测试 |

### 10.4 全局回滚开关速查

| 故障场景 | 一键回滚动作 |
|---|---|
| 批 1 配置读取错误 | yaml 删 `intent_routing` 段 |
| 批 2 L1 误命中率上升 | 移除新增同义词（编辑 `_RULE_LIBRARY`）|
| 批 3 L2 投票异常 | yaml `l2_topk: 1` |
| 批 3 length_ratio 折扣弱化导致误命中 | yaml `l2_length_ratio_penalty: 0.7` |
| 批 4 L2.5 错误复用 DAG | yaml `l25_enabled: false` |
| 批 4 case_store 写入异常 | DAGExecutor 接入处加 `if settings.intent_routing.case_store_write_enabled` 守卫（需新增 flag）|
| 批 5 通用模板执行失败 | yaml `generic_template_enabled: false` |
| 批 5 模板节点错误 | 在模板定义中临时移除该节点 + 重启 |

### 10.5 监控复盘节奏

| 节奏 | 动作 |
|---|---|
| 每日（前两周） | 看 `GET /admin/metrics/route-hit-rate?window=1d`，关注 DAG 触发率、ReAct 触发率 |
| 每周 | 7 天分布对比，决定是否切下一档 |
| 每月 | 抽样 50 条 DAG 报告人工评分（正确性 / 完整性 / 可读性）|
| 季度 | 评估是否满足"ReAct 触发率下降 30~45pp"目标，决定是否启动后续专题（如模型化路由）|

---

## 11. 附录：术语表

| 术语 | 全称/别名 | 含义 | 出处 |
|---|---|---|---|
| **L1** | Level 1 | 关键词命中率匹配（hit_rate = 命中关键词数 / 规则总关键词数）| [router.py `_try_level1`](../core/orchestrator/router.py) |
| **L2** | Level 2 | Chroma 种子库语义匹配，cosine similarity = 1 - distance | [router.py `_try_level2`](../core/orchestrator/router.py) |
| **L2.5** | Level 2.5（本专题新增）| 历史成功 DAG 案例检索，命中后直接复用 dag_definition | 本文档 7.1 节 |
| **L3** | Level 3 | LLM 二段分类，输出 intent_kind + analysis_type | [router.py `_try_level3`](../core/orchestrator/router.py) |
| **bypass** | 前置 bypass | CHITCHAT/META/RECALL 直接生成 sentinel signal，跳过 L1/L2/L3 | [router.py `_classify_bypass`](../core/orchestrator/router.py) |
| **DAG** | Directed Acyclic Graph | 静态任务编排路径，由 `dag/templates.py` 加载预定义任务图，并行执行 | [dag/executor.py](../core/orchestrator/dag/executor.py) |
| **ReAct** | Reasoning + Acting | LangChain Agent 自由探索路径，LLM 自主调用 tools | [modules/p2p/agent.py](../modules/p2p/agent.py) |
| **IntentKind** | 意图大类 | ANALYSIS / DATA_LOOKUP / CLARIFICATION / META / CHITCHAT / OUT_OF_SCOPE / RECALL，7 类 | [signal.py `IntentKind`](../core/orchestrator/signal.py) |
| **AnalysisType** | 分析子类型 | THREE_WAY_MATCH / PRICE_VARIANCE / ... / COMPREHENSIVE，11 类 | [api/schemas/analysis.py](../api/schemas/analysis.py) |
| **QuerySignal** | 查询信号 | 路由层输出的中间结构，含 intent_kind / keywords / entities / route_level / confidence | [signal.py `QuerySignal`](../core/orchestrator/signal.py) |
| **case_store** | DAG 案例存储 | 成功 DAG 案例的持久化层（PG 权威 + Chroma 缓存）| [dag/case_store.py](../core/orchestrator/dag/case_store.py) |
| **DATA_LOOKUP** | 事实查询意图 | "查最新 PO" / "列出 SUP-001 的发票" 等纯检索查询，强制走 ReAct（无 DAG 模板）| [signal.py](../core/orchestrator/signal.py) |
| **CLARIFICATION** | 澄清追问 | 采购意图明确但缺关键参数，由 orchestrator 模板渲染追问，不触发分析 | [orchestrator.py `_render_intent_kind_template`](../core/orchestrator/orchestrator.py) |
| **COMPREHENSIVE 无实体** | 无实体的综合分析 | analysis_type=COMPREHENSIVE 且 query 中未提取出 PO/供应商等实体；当前走 ReAct，本专题批 5 部分收编 | 本文档 2.1 节 |
| **route_level** | 路由层级 | 0=bypass, 1=L1, 2=L2, 25=L2.5（新）, 3=L3 | [signal.py](../core/orchestrator/signal.py) |
| **intent_kind** | 意图大类（字段名）| QuerySignal 字段，存 IntentKind 枚举 | 同上 |
| **execution** | 执行路径 | 本专题批 1 新增的 trace span 属性，取值 "dag" / "react" | 本文档 4.2 节步骤 1.6 |
| **confidence** | 置信度 | 0.0-1.0 浮点数；L1/L2 来自匹配分数，L3 由 LLM 输出 | [router.py](../core/orchestrator/router.py) |
| **length_ratio** | 长度比 | query 长度 / seed 长度，过低时 L2 相似度打折避免短查询虚高 | [router.py](../core/orchestrator/router.py) |
| **top-k 投票** | top-k voting | 取 Chroma top-k 结果，按 analysis_type 分桶加权投票 | 本文档 6.2 节步骤 3.2 |
| **rank_weight** | 排序权重 | top-k 投票中每个排名的权重，默认 [1.0, 0.6, 0.3] | 本文档 6.2 节步骤 3.2 |
| **dag_definition** | DAG 定义 | list[dict] 形式的任务列表，每个 task 含 task_id/tool/depends_on/params | [dag/templates.py](../core/orchestrator/dag/templates.py) |
| **trace span** | 追踪跨度 | OpenTelemetry-style 单次操作记录，含 name/attributes/start_time/end_time | [observability/middleware.py](../core/observability/middleware.py) |
| **feature flag** | 特性开关 | 配置中的布尔字段，控制某能力是否启用，便于灰度与回滚 | 本文档 4.2 节步骤 1.1 |
| **保守档 / 激进档** | conservative / aggressive | 阈值的两组建议值，保守=当前等价，激进=优化目标 | 本文档 4.2 节步骤 1.1 表格 |
| **sentinel keyword** | 哨兵关键词 | 写入 `signal.keywords[0]` 的特殊字符串（"data_lookup"/"meta"/"chitchat"等），区别于 AnalysisType 枚举值 | [router.py](../core/orchestrator/router.py) |
| **早退 / early-return** | early return | orchestrator 检测到 CHITCHAT/META/CLARIFICATION/OUT_OF_SCOPE 时，直接返回模板响应，不触发 DAG/ReAct | [orchestrator.py](../core/orchestrator/orchestrator.py) |
| **冷启动** | cold start | 系统刚上线、案例库为空时的状态；L2.5 此时直接 fallback 到 L3 | 本文档 7.4 节 |

---

## 文档版本历史

| 版本 | 日期 | 改动 |
|---|---|---|
| v1.0 | 2026-04-16 | 初稿：覆盖 5 批次方案 + punt 决策 + 验证回滚 + 术语表 |
| v1.1 | 2026-04-16 | 增补：DATA_LOOKUP 模板化分析（第 12-15 章）+ 与"DAG 模板扩展"专题的关系（D 节）|

---

## 12. DATA_LOOKUP 流量与 ReAct 痛点（追加）

> 本章为 v1.1 增补，回应"是否要把 PO/AP 等实体的查询能力做成独立查询模板"的设计问题。
> 配套的"P2P 分析模板扩展"分析见独立文档 [dag_template_expansion_analysis.md](dag_template_expansion_analysis.md)。

### 12.1 当前 DATA_LOOKUP 的处理路径

[orchestrator.py](../core/orchestrator/orchestrator.py) `use_dag` 决策中，`intent_kind == DATA_LOOKUP` **强制走 ReAct**——这是 intent_kind 改造时的设计选择，原因：
- 事实查询无 DAG 模板（`load_dag_template` 不接受 DATA_LOOKUP）
- ReAct 可以让 LLM 自由组合 query_* tools（如"查 SUP-001 最新 5 笔发票"→ LLM 自己挑 `query_invoices` 并加排序条件）

### 12.2 ReAct 在事实查询场景的痛点

| 痛点 | 说明 | 严重度 |
|---|---|---|
| **延迟高** | ReAct 至少跑 1 轮 LLM "Thought → Action → Observation"；事实查询本不需要推理 | 高（P50 +1-3s）|
| **不可预测** | LLM 可能多调几个无关 tools（如查 PO 还顺手查发票），消耗额外时间和 token | 中 |
| **报告不必要** | 用户问"最新 PO 是什么"，得到 Markdown 报告而不是结构化数据列表，UI 不便展示 | 中 |
| **不可缓存** | 同一查询每次都重跑 LLM，无法走批 4 的 case_store 复用（case_store 只存 DAG 案例）| 高 |
| **可观测性弱** | ReAct 内部 tool 调用顺序由 LLM 决定，trace 不稳定，难以做命中率分析 | 中 |
| **成本高** | 每次至少 1 次 LLM 调用，事实查询本可零 LLM | 高 |

### 12.3 DATA_LOOKUP 流量占比估算

**当前缺乏精确数据**（监控未上线）。基于 intent_seeds.yaml 中 data_lookup 类目的种子数（15 条）vs 总 145 条，以及 P2P 业务直觉：

| 假设条件 | DATA_LOOKUP 占总流量比 |
|---|---|
| 保守（仅明显的"查 X"句式）| 5-10% |
| 中等（含模糊"看下 X"）| 10-20% |
| 激进（含所有事实型询问）| 20-30% |

**关键判断**：即便取保守 10%，DATA_LOOKUP 100% 走 ReAct 意味着 P50 路由延迟被这部分流量拖累、且无任何缓存可能。

### 12.4 重复度估算

DATA_LOOKUP 查询有强重复倾向（运营场景常用，如每日"查最新订单"）。预估同 query_hash 出现频次分布：
- top 10% 高频 query：可能贡献 50-70% 的 DATA_LOOKUP 流量
- 这部分如果模板化 + 缓存，效果立竿见影

### 12.5 现状结论

DATA_LOOKUP **不是不能优化**，当前"强制 ReAct"是 intent_kind 改造期的过渡设计。本专题应正面回答：是否要把这部分流量从 ReAct 拆出来？怎么拆？

---












