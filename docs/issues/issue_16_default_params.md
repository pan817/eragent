# Issue #16: 默认参数值全面审查与 LLM 统一默认值方案

> 发现日期：2026-05-02 | 分析日期：2026-05-04

---

## 1. 现状问题与参数流转分析

### 1.1 问题本质

当前系统中有 5 个关键默认参数值散落在 5 个不同的代码层次中，由各层独立填充和覆盖。这导致：

1. **分支逻辑复杂**：`orchestrator.py:594-624` 用 4 层 if/elif 判断时间窗口，每新增一个"应该放宽时间窗口"的场景就要追加分支，违反开闭原则。
2. **边界场景遗漏**：已知 bug——"有供应商名称但无编号"的模糊查询仍受 30 天限制，查不到目标数据。
3. **语义割裂**：时间窗口的合理值强依赖查询语义（"分析供应商绩效"需要 180 天，"查最新 PO"不需要时间窗口），但决策被硬编码在 Python 分支中而非由理解语义的 LLM 判断。

### 1.2 默认值散落的 5 个层次

| 层次 | 位置 | 涉及参数 | 决策方式 |
|------|------|----------|----------|
| **L1: API Schema** | `api/schemas/analysis.py:55-70` | `analyst_role="general"`, `output_mode="auto"` | 前端未传时 Pydantic 填充 |
| **L2: 配置文件** | `config/settings.py:263,532` | `default_time_range_days=30`, `search_default_days=30` | Python 硬编码 |
| **L3: Unified Router LLM** | `unified_router.py:72-76` | `days`, `limit`, `order_by` | LLM 从查询中提取；未识别时返回 null |
| **L4: Orchestrator 分支** | `orchestrator.py:594-624` | `time_range_days` 最终值 | 4 层 if/elif 填充默认值 |
| **L5: Orchestrator output_mode** | `orchestrator.py:836-858` | `effective_output_mode` | auto 时按 intent_kind 映射 |

### 1.3 参数流转链路

```
用户请求 (AnalysisRequest)
│
├─ time_range / time_range_days ─────────────────────────────────┐
├─ analyst_role="general"                                        │
├─ output_mode="auto"                                            │
│                                                                │
▼                                                                │
Unified Router LLM (一次调用)                                     │
│  输入: query + analyst_role + session_entities                  │
│  输出: QuerySignal {                                            │
│    intent_kind, keywords, entities,                             │
│    time_range_days (用户显式指定才有值, 否则 null),               │
│    is_cross_entity, resolved_query                              │
│  }                                                              │
│  ※ LLM 不输出推荐默认值，仅提取用户显式参数                       │
│                                                                │
▼                                                                │
Orchestrator 参数合并 (orchestrator.py:594-624)                   │
│  优先级: request.time_range > request.time_range_days            │
│          > signal.time_range_days > 分支判断 > config 默认        │
│                                                                │
│  分支判断 (问题所在):                                            │
│  ├─ has_specific_entity? → days=0 (不限)                        │
│  ├─ is_data_lookup + has_ranking_constraint? → days=0           │
│  └─ else → settings.analysis.default_time_range_days (=30)  ◄──┘
│     ↑ 供应商名称、模糊描述等场景落入此分支，被 30 天误限
│
▼
Orchestrator output_mode 解析 (orchestrator.py:836-858)
│  ├─ 显式 (detailed/brief/table/chat) → 直接使用
│  └─ auto →
│       ├─ data_lookup → chat
│       └─ 其他 → detailed
│       ※ 不考虑 analyst_role（management 也得到 detailed）
│
▼
执行路径 (DAG / Plan-and-Solve / ReAct)
```

### 1.4 已知 bug 场景

| 场景 | 用户查询示例 | 期望行为 | 实际行为 | 根因 |
|------|-------------|----------|----------|------|
| mock 数据超 30 天 | "分析 PO-2024-001 的三路匹配" | 查到该 PO | 无结果 | `has_specific_entity` 仅检查 entity_keys，不覆盖 mock 数据时间戳场景；已有 `days=0` 短路但仅 entity_keys 命中 |
| 供应商名称查询 | "分析华为供应商的绩效" | 覆盖 90-180 天 | 仅查 30 天 | "华为"不在 entity_keys 中，落入 else 分支 |
| 长周期分析 | "供应商绩效趋势" | 覆盖 180+ 天 | 仅查 30 天 | 无实体、无排序约束，走 config 默认 |
| 角色差异输出 | management 用户查询 | brief 摘要 | detailed 长报告 | output_mode auto 不感知 analyst_role |

---

## 2. 五个默认值逐项评估

### 2.1 `AnalysisSettings.default_time_range_days = 30`

- **位置**：`config/settings.py:263`
- **结论**：**改为 90** + 引入 LLM `recommended_days` 推荐机制
- **理由**：
  - 30 天对供应商绩效（通常看季度/半年）、价格趋势（需跨月对比）过于狭窄。
  - 90 天覆盖一个完整季度，是 ERP 采购分析的常见最小有意义窗口。
  - 作为兜底值，90 天在数据量（查询性能）和业务覆盖度之间取得平衡。
  - 更精确的时间窗口由 LLM 根据查询语义推荐（见第 3 章方案设计）。
- **风险**：查询数据量增大约 3 倍，需关注慢查询。现有 `db_io_timeout_seconds=30s` 可兜底。

### 2.2 `ChatHistorySettings.search_default_days = 30`

- **位置**：`config/settings.py:532`
- **结论**：**改为 90**
- **理由**：
  - 跨会话历史搜索 30 天窗口过短，用户经常引用"上个月的分析""上次讨论的供应商"。
  - 90 天与 `default_time_range_days` 保持一致，减少认知负担。
  - `search_max_days=365` 不变，90 天是默认搜索范围而非上限。
- **风险**：向量搜索范围增大，但有 `search_timeout_seconds=0.2s` 和 `search_max_limit=10` 限制，影响可控。

### 2.3 `AnalysisRequest.analyst_role = "general"`

- **位置**：`api/schemas/analysis.py:55-58`
- **结论**：**保持 "general" 不变**，添加注释标注应由前端登录态传入
- **理由**：
  - `analyst_role` 是用户身份属性，不是查询语义属性，不适合交给 LLM 推断。
  - 当前 LLM 已在 prompt 中接收 `analyst_role` 并据此调整分类偏好（`ROLE_DESCRIPTIONS`），机制已正确。
  - 自动填充需前端集成登录态，属于前端改造范畴，后端 API 保持现有默认值即可。
  - 后续前端对接时，前端从用户 profile 读取角色并在请求中传入。
- **风险**：无。保持现状，仅补充文档说明。

### 2.4 `AnalysisRequest.output_mode = "auto"`

- **位置**：`api/schemas/analysis.py:60-70`
- **结论**：**保持 "auto" 不变**，在 auto 解析逻辑中引入 LLM `recommended_output_mode`
- **理由**：
  - "auto" 作为默认值是正确的——让系统根据查询性质决策。
  - 当前 auto 解析过于简单（仅按 intent_kind 二分：data_lookup→chat / 其他→detailed），不感知 analyst_role 和查询复杂度。
  - LLM 已理解查询语义和 analyst_role，适合推荐 output_mode（如 management 角色偏好 brief）。
  - 解析优先级：显式非 auto > LLM recommended > 现有 intent_kind 映射。DAG 降级保护不变。
- **风险**：LLM 推荐值可能与执行路径冲突（如推荐 chat 但走了 DAG）。已有 DAG 降级保护兜底。

### 2.5 `IntentRoutingSettings.l3_dag_min_confidence = 0.5`

- **位置**：`config/settings.py:430-433`
- **结论**：**保持 0.5 不变**
- **理由**：
  - 技术债描述中原值为 0.75，但代码实际已是 0.5（已在此前的迭代中调整）。
  - 0.5 是合理阈值：低于 0.5 的 ANALYSIS 查询意图确实模糊，走 ReAct 让 Agent 自主决策比强制 DAG 更安全。
  - 该参数属于系统调参范畴（需结合线上路由命中率日志统计），不适合交给 LLM。
  - 当前无线上数据表明 0.5 不合理，保持不变，待有数据后按需调整。
- **风险**：无。

### 2.6 评估总结

| 参数 | 当前值 | 目标值 | 改动类型 |
|------|--------|--------|----------|
| `default_time_range_days` | 30 | 90 + LLM recommended | 配置调整 + 新机制 |
| `search_default_days` | 30 | 90 | 配置调整 |
| `analyst_role` | "general" | "general"（不变） | 仅补注释 |
| `output_mode` auto 解析 | intent_kind 二分 | + LLM recommended | 新机制 |
| `l3_dag_min_confidence` | 0.5 | 0.5（不变） | 无改动 |

---

## 3. LLM 统一默认值方案设计

### 3.1 设计原则：哪些交 LLM，哪些不交

| 判断维度 | 交给 LLM | 留在配置/代码 |
|----------|----------|--------------|
| 决策依赖查询语义 | `recommended_days`, `recommended_output_mode` | — |
| 用户身份属性 | — | `analyst_role`（前端传入） |
| 系统调参 | — | `l3_dag_min_confidence`（运维配置） |
| 基础设施配置 | — | `search_default_days`（与查询语义无关） |

**核心思路**：Unified Router LLM 在完成意图分类+参数提取的同一次调用中，额外输出两个"推荐默认值"字段。这些推荐值仅在用户未显式指定对应参数时生效，且受配置层硬上限钳位，不增加 LLM 调用次数。

### 3.2 新增字段定义

#### QuerySignal 新增字段（`core/orchestrator/signal.py`）

```python
recommended_days: int | None = None
"""LLM 根据查询语义推荐的时间窗口（天）。
仅在用户未显式指定时间范围时生效。None 表示 LLM 未给出推荐。"""

recommended_output_mode: str | None = None
"""LLM 根据查询性质和 analyst_role 推荐的输出模式。
仅在 output_mode="auto" 时生效。None 表示 LLM 未给出推荐。"""
```

#### LLM JSON 输出新增字段

在现有输出格式基础上追加：

```json
{
  "...现有字段...",
  "recommended_days": 90,
  "recommended_output_mode": "brief"
}
```

### 3.3 Prompt 改动要点

在 `_UNIFIED_PROMPT` 的"参数提取"段之后，新增"默认参数推断"段：

```
## 默认参数推断（当用户未显式指定时）
当 days 为 null（用户未指定时间范围）时，根据查询语义推断 recommended_days：
- 有明确实体编号（PO-xxx/SUP-xxx/INV-xxx 等）→ 0（不限时间，精确查找）
- data_lookup + 有 limit/order_by（"查最新N个"）→ 0（按排序取 Top-N）
- 供应商绩效/趋势分析 → 180（需跨季度数据）
- 三路匹配/价格差异/付款合规 → 90（需覆盖完整季度）
- 一般性分析/综合分析 → 90（默认覆盖一个季度）
- 用户未指定时间但提及具体供应商名称/物料描述 → 180（模糊查询需宽窗口）

当 output_mode 未由前端显式指定（即系统处于 auto 模式）时，推断 recommended_output_mode：
- data_lookup（事实查询）→ "chat"
- analyst_role=management → "brief"
- 简单单实体分析 → "brief"
- 复杂多维度分析 → "detailed"
- 未能判断 → null（交给后端按 intent_kind 映射）
```

#### 输出格式更新

```json
{"intent_kind":"...","type":"...","confidence":0.0,"is_cross_entity":false,
 "resolved_query":"...","missing_params":[],
 "po_number":null,"vendor_id":null,"invoice_num":null,
 "check_number":null,"receipt_number":null,
 "days":null,"limit":null,"order_by":null,
 "recommended_days":90,"recommended_output_mode":null}
```

### 3.4 三级优先级链

#### 时间窗口（`time_range_days`）

```
L1: 显式指定（explicit）
    request.time_range ("7d"/"this_month") 解析为天数
    → request.time_range_days (API 传入整数)
    → signal.time_range_days (LLM 从用户查询中提取的显式天数)
    任一有值即采用，不继续往下。

L2: LLM 推荐（recommended）
    signal.recommended_days
    LLM 根据查询语义给出的建议值。
    校验：必须 > 0 且 ≤ max_time_range_days (365)，否则忽略。
    特殊值：0 = 不限时间（精确查找场景）。

L3: 配置兜底（config fallback）
    settings.analysis.default_time_range_days (=90)
    仅当 L1、L2 均无有效值时使用。
```

**对比现状的简化**：移除 orchestrator 中的 `has_specific_entity` / `has_ranking_constraint` 分支判断。这些语义判断由 LLM 在 L2 中统一处理——LLM 看到"分析 PO-2024-001"自然输出 `recommended_days=0`，看到"查最新 5 个 PO"自然输出 `recommended_days=0`。

#### 输出模式（`output_mode`）

```
L1: 前端显式（explicit）
    request.output_mode ≠ "auto" → 直接使用，不覆盖。

L2: LLM 推荐（recommended）
    signal.recommended_output_mode（仅 output_mode="auto" 时消费）
    校验：必须在 {"detailed","brief","table","chat"} 白名单中，否则忽略。

L3: intent_kind 映射（现有逻辑保留作兜底）
    data_lookup → "chat"
    其他 → "detailed"

路径降级保护（不变）：
    (DAG || Plan-and-Solve) && effective="chat" → 强制升 "brief"
```

### 3.5 校验与钳位机制

在 `_parse_unified_response` 中对新字段做硬校验：

```python
# recommended_days: 钳位到 [0, max_time_range_days]
rec_days = data.get("recommended_days")
if rec_days is not None:
    try:
        rd = int(rec_days)
        if rd < 0:
            rd = None  # 非法值忽略
        elif rd > max_time_range_days:
            rd = max_time_range_days  # 钳位到上限
        recommended_days = rd
    except (TypeError, ValueError):
        recommended_days = None

# recommended_output_mode: 白名单校验
rec_mode = data.get("recommended_output_mode")
if rec_mode and isinstance(rec_mode, str) and rec_mode in _VALID_OUTPUT_MODES:
    recommended_output_mode = rec_mode
else:
    recommended_output_mode = None
```

### 3.6 兜底策略

| 故障场景 | 兜底行为 |
|----------|----------|
| LLM 调用失败 | `_fallback_signal` 返回 `recommended_days=None, recommended_output_mode=None`，走 L3 配置兜底 |
| JSON 解析失败 | 同上 |
| `recommended_days` 非法值 | 忽略（设为 None），走 L3 |
| `recommended_output_mode` 非白名单 | 忽略（设为 None），走 L3 intent_kind 映射 |
| LLM 幻觉（如 days=99999） | 钳位到 `max_time_range_days=365` |

**关键保证**：任何 LLM 故障或异常输出都不会导致比现状更差的行为——最坏情况回退到 `config.default_time_range_days=90`（比现在的 30 天还更宽）。

---

## 4. 涉及文件与变更清单

### 4.1 T2.1: Unified Router prompt 改造

**文件**：`core/orchestrator/unified_router.py`

| 行号 | 变更 |
|------|------|
| 72-76 | "参数提取"段末尾追加 `recommended_days` 和 `recommended_output_mode` 的提取规则说明 |
| 84-85 | JSON 输出格式模板追加两个新字段 |
| 87-93 | 示例追加：每个示例补充 `recommended_days` 和 `recommended_output_mode` 值 |

### 4.2 T2.2: 解析逻辑改造

**文件**：`core/orchestrator/unified_router.py`

| 行号 | 变更 |
|------|------|
| 98-215 (`_parse_unified_response`) | 在 `order_by` 解析之后追加 `recommended_days`（int 校验 + 钳位 `[0, 365]`）和 `recommended_output_mode`（白名单校验）解析逻辑 |
| 203-215 | `QuerySignal` 构造追加两个新字段赋值 |
| 218-227 (`_fallback_signal`) | 兜底信号中两个新字段保持 `None`（dataclass 默认值即可，无需显式赋值） |

### 4.3 T2.3: Signal 字段扩展

**文件**：`core/orchestrator/signal.py`

| 行号 | 变更 |
|------|------|
| 83 行之后 | 新增 `recommended_days: int | None = None` 和 `recommended_output_mode: str | None = None` 两个字段及 docstring |

### 4.4 T3.1: Orchestrator 时间窗口重构

**文件**：`core/orchestrator/orchestrator.py`

| 行号 | 变更 |
|------|------|
| 594-624 | **删除** `has_specific_entity` / `has_ranking_constraint` 分支判断。替换为三级优先级链：`explicit_days` 有值 → 采用；否则 `signal.recommended_days is not None` → 采用（含 0 = 不限时间）；否则 → `settings.analysis.default_time_range_days` |

重构前后对比：

```python
# ── 重构前（4 层 if/elif）──
if explicit_days:
    time_range_days = explicit_days
else:
    has_specific_entity = any(signal.entities.get(k) for k in _entity_keys)
    has_ranking_constraint = bool(signal.entities.get("limit") or signal.entities.get("order_by"))
    if has_specific_entity:
        time_range_days = 0
    elif _is_lookup and has_ranking_constraint:
        time_range_days = 0
    else:
        time_range_days = self._settings.analysis.default_time_range_days

# ── 重构后（三级优先级）──
if explicit_days:
    time_range_days = explicit_days
elif signal.recommended_days is not None:
    time_range_days = signal.recommended_days
else:
    time_range_days = self._settings.analysis.default_time_range_days
```

### 4.5 T3.2: Orchestrator output_mode 重构

**文件**：`core/orchestrator/orchestrator.py`

| 行号 | 变更 |
|------|------|
| 838-845 | `auto` 分支中，先检查 `signal.recommended_output_mode`，有值且合法则采用；否则保留现有 `intent_kind` 映射作兜底 |

重构前后对比：

```python
# ── 重构前 ──
if effective_output_mode == "auto":
    if is_data_lookup:
        effective_output_mode = "chat"
    else:
        effective_output_mode = "detailed"

# ── 重构后 ──
if effective_output_mode == "auto":
    if signal.recommended_output_mode:
        effective_output_mode = signal.recommended_output_mode
    elif is_data_lookup:
        effective_output_mode = "chat"
    else:
        effective_output_mode = "detailed"
```

### 4.6 T4: 静态默认值调整

| 文件 | 行号 | 变更 |
|------|------|------|
| `config/settings.py` | 263 | `default_time_range_days: int = 30` → `90`，移除 TECH-DEBT 注释 |
| `config/settings.py` | 532 | `search_default_days: int = 30` → `90` |
| `api/schemas/analysis.py` | 55-58 | `analyst_role` 字段的 `description` 追加"建议前端从用户登录态自动填充" |

### 4.7 T5: 测试覆盖

| 文件 | 新增测试 |
|------|----------|
| `tests/unit/test_router.py` | `TestParseUnifiedResponse`: recommended_days 正常解析、null→None、负值→None、超 365→钳位 365、非整数→None；recommended_output_mode 白名单校验、非法值→None |
| `tests/unit/test_orchestrator.py` | `TestTimeRangePriority`: explicit > recommended > fallback 三级验证；recommended_days=0 短路验证；`TestOutputModePriority`: recommended 消费验证、DAG 降级保护不变 |
| `tests/unit/test_orchestrator.py` | `TestDefaultValueRegression`: default_time_range_days=90 生效验证、search_default_days=90 生效验证 |

### 4.8 T6: 技术债清理

| 文件 | 变更 |
|------|------|
| `config/settings.py:263` | 移除 `# TECH-DEBT(#16): ...` 注释（T4 已包含） |
| `docs/issues/agent_issue.md` | #16 条目标注"已修复"，引用本文档和修复 commit |
