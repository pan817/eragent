# P2P Agent 记忆管理体系设计

> 基于 ERP 采购分析 Agent 业务场景的完整记忆架构设计。涵盖短期（会话内）+ 长期（跨会话）+ 整合（后台维护）三层体系。

---

## 设计哲学

> 以分析正确性为核心，用结构化类型约束记忆内容，用精确优先保障召回质量，用异步化换取零延迟体验，用多层降级确保记忆系统永远不成为分析的负担。

### 1. 记忆是锦上添花，不是必要条件

记忆系统的任何环节失败，分析主流程都不受影响。与参考文档（cc-haha）的哲学一致，但更极端——cc-haha 的记忆写入是"用户感知的功能"（用户说"记住 X"），我们的记忆写入对用户完全透明。分析结果的正确性来自数据 + 规则 + LLM，不来自记忆。记忆只提供"更好的上下文"。

### 2. ERP 场景下精确性优先于召回率

这是和参考文档最大的分歧。cc-haha 用 Sonnet 做语义选择器（200 个文件中选 5 个），因为编程助手的记忆天然模糊（"用户偏好简洁风格"）。ERP 不同——把 SUP-003 的修正应用到 SUP-004，比漏召更危险。所以对 entity_profile、correction、domain_fact 三类采用精确检索优先（entity_id + analysis_type），语义检索只作补充。

### 3. 写入异步、检索同步、整合后台

三个时间敏感度不同的操作，用三种执行模式：
- **写入**（提取 / 反馈 / 摘要）：用户不需要等记忆写完才看到分析结果 → fire-and-forget
- **检索**（注入 prompt）：分析必须带着记忆上下文启动 → 同步但有超时降级（200ms）
- **整合**（合并 / 淘汰）：完全后台，用户无感知 → 异步任务

### 4. 类型封闭，用结构约束 LLM 的漂移

5 种类型是封闭枚举，不允许 LLM 自由分类。参考文档的 4 类（user / feedback / project / reference）是通用编程助手视角，本设计根据 ERP 业务重新定义了 5 类（user_preference / entity_profile / analysis_insight / correction / domain_fact）。每类有明确的写入来源、TTL、整合规则、检索策略——类型不是标签，是行为契约。

### 5. 记忆有生命周期，不是只增不减

参考文档用文件系统 + 200 文件 cap 控制规模。本设计用三层淘汰：
- **TTL**：业务数据有时效性，90 天前的供应商画像可能已过时
- **Cap**：per-user 200 条上限，整合产物受保护优先保留
- **整合**：多条碎片记忆 → 一条精华记忆，信息密度上升，条数下降

不维护的记忆会腐烂。整合机制（20 条 / 72h 触发）相当于参考文档的 autoDream——"晚上整理笔记本"。

### 6. PostgreSQL 是唯一事实源，不是文件系统

参考文档坚持"文件系统 SOT"——用户可以 vim 编辑记忆文件。这对 CLI 工具合理，但 ERP agent 是服务端多用户部署，文件系统不可行。本设计用 PostgreSQL 做 SOT，向量索引（Chroma）做加速旁路，向量丢失不影响正确性（退化为 FTS only）。

---

## A. 架构总览

### A1. 三层记忆架构

```
┌─────────────────────────────────────────────────────────────────────┐
│ 第 1 层：短期记忆（会话内）                                            │
│                                                                     │
│  LangGraph PostgresSaver Checkpointer                               │
│    ├─ 对话历史（HumanMessage / AIMessage / ToolMessage）              │
│    ├─ 滑动窗口截断（max_messages，截断前实体保全）                      │
│    └─ LLM 异步摘要压缩（分析结果 → 结构化摘要，可配置开关）              │
│                                                                     │
│  Session Entities（session_entities 表）                              │
│    └─ 跨轮实体上下文（po_number / supplier_id 等，指代消解用）           │
│                                                                     │
│  ReAct 裁剪（MemoryMiddleware）                                       │
│    └─ 早期 ToolMessage 内容截断（不修改 checkpoint）                    │
│                                                                     │
│  寿命：一次会话（session_id 隔离）                                      │
│  存储：PostgreSQL（checkpointer + session_entities）                   │
└─────────────────────────────────────────────────────────────────────┘
                              ↑↓
┌─────────────────────────────────────────────────────────────────────┐
│ 第 2 层：长期记忆（跨会话）                                            │
│                                                                     │
│  memories 表（PostgreSQL）+ Chroma 向量索引（可选）                     │
│    ├─ user_preference    — 用户分析偏好                                │
│    ├─ entity_profile     — 业务实体累积画像                             │
│    ├─ analysis_insight   — 跨次分析趋势/模式                           │
│    ├─ correction         — 用户对分析结果的修正                         │
│    └─ domain_fact        — 业务领域事实知识                             │
│                                                                     │
│  reports 表（PostgreSQL）+ Chroma 向量索引（可选）                      │
│    └─ 分析报告（审计资产，无淘汰）                                       │
│                                                                     │
│  写入：分析完成后结构化提取 + 用户反馈 LLM 提取                          │
│  读取：Hybrid 召回（稀疏 FTS + 稠密向量 + RRF 融合）                    │
│  注入：按类型优先级分配 token 预算，注入 LLM prompt                      │
│  寿命：用户级（user_id 隔离），按类型 TTL 淘汰                           │
│  存储：PostgreSQL + Chroma                                            │
└─────────────────────────────────────────────────────────────────────┘
                              ↑↓
┌─────────────────────────────────────────────────────────────────────┐
│ 第 3 层：记忆整合（后台维护）                                           │
│                                                                     │
│  触发条件：每用户 20 条新记忆 或 距上次整合 72h（可配置）                  │
│                                                                     │
│  整合动作：                                                           │
│    ├─ entity_profile 按实体 ID 合并（保留最新指标，追加历史趋势）         │
│    ├─ analysis_insight 归纳（同类发现 → 合并为趋势）                    │
│    ├─ correction 与 domain_fact 冲突检测                               │
│    ├─ 超龄记忆 TTL 淘汰                                               │
│    └─ LLM 语义合并（可配置开关，关闭时走纯规则合并）                     │
│                                                                     │
│  执行：异步后台任务（复用 TaskRegistry）                                 │
│  锁：per-user 整合锁（防并发）                                         │
│  存储：整合状态记录在 memory_consolidation_log 表                       │
└─────────────────────────────────────────────────────────────────────┘
```

**三层关系：**

- **数据方向**：第 1 层的分析结果经提取写入第 2 层；第 2 层的相关记忆注入第 1 层的 prompt；第 3 层定期整合第 2 层的存量记忆
- **寿命递增**：第 1 层（会话）→ 第 2 层（用户级，按 TTL）→ 第 3 层产出的整合记忆（用户级，更长 TTL）
- **更新成本递增**：第 1 层（同步写 checkpoint）→ 第 2 层（同步/异步写 DB + 向量）→ 第 3 层（异步 LLM 调用 + DB 批量更新）
- **SOT 哲学**：PostgreSQL 是唯一事实源。与参考文档的"文件系统 SOT"不同——ERP agent 是服务端多用户部署，数据库是唯一合理选择

---

### A1.1 MemoryManager — 记忆系统统一入口

#### 设计动机

设计规格要求"记忆的入口要抽象，实现逻辑要内聚，和其他逻辑模块解耦"。本设计新增了 MemoryExtractor、FeedbackDetector、Consolidation、Injection 四个模块，如果 Orchestrator 直接依赖这 6 个记忆模块，会导致编排器臃肿、协调逻辑外泄。

MemoryManager 是所有记忆操作的**唯一入口**。Orchestrator 只依赖 MemoryManager 一个类。

#### 职责边界

```
MemoryManager（薄委托层，不含业务逻辑）
  │
  ├─ 持有的内部组件：
  │    ├─ ShortTermMemory         — checkpointer / 摘要 / 实体上下文
  │    ├─ MemoryRepository        — 长期记忆读写
  │    ├─ ReportRepository        — 报告持久化
  │    ├─ MemoryExtractor         — 分析结果 → 结构化记忆
  │    ├─ FeedbackDetector        — 用户反馈检测 + LLM 提取
  │    └─ MemoryInjector          — 检索 + 格式化注入
  │
  └─ 暴露给 Orchestrator 的接口（5 个方法）：
       ├─ build_context()           — 分析前：检索长期记忆 + 格式化注入（同步，带超时）
       ├─ on_analysis_complete()    — 分析后：fire-and-forget 提取 + 摘要 + 整合检查
       ├─ on_user_message()         — 每轮：fire-and-forget 反馈检测
       ├─ load_session()            — 会话开始：加载短期记忆上下文
       └─ save_session_entities()   — 每轮：写入实体上下文
```

#### 接口设计

```python
# core/memory/manager.py（新增文件）

class MemoryManager:
    """记忆系统统一入口。

    Orchestrator 只通过此类与记忆系统交互。
    内部协调短期记忆、长期记忆、提取、检测、整合、摘要等组件。
    所有异步操作在内部 fire-and-forget，不向调用方泄露。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._short_term = ShortTermMemory(settings)
        self._mem_repo: MemoryRepository | None = None   # 懒初始化
        self._extractor: MemoryExtractor | None = None    # 懒初始化
        self._feedback: FeedbackDetector | None = None    # 懒初始化

    # ── 分析前：构建记忆上下文 ──────────────────────────────

    async def build_context(
        self,
        user_id: str,
        query: str,
        parsed_params: dict[str, Any],
        analysis_type: str | None = None,
    ) -> str:
        """检索长期记忆并格式化为 prompt 注入文本。

        同步执行（结果要注入分析 prompt），带超时保护。
        超时或失败返回空字符串，不阻塞分析。
        """
        if not self._settings.memory.long_term_enabled:
            return ""
        try:
            return await asyncio.wait_for(
                self._search_and_format(user_id, query, parsed_params, analysis_type),
                timeout=self._settings.memory.long_term_search_timeout_seconds,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            _logger.warning("memory context build failed: %s", exc)
            return ""

    # ── 分析后：提取 + 摘要 + 整合检查 ─────────────────────

    def on_analysis_complete(
        self,
        result: AnalysisResult,
        session_id: str,
        parsed_params: dict[str, Any],
    ) -> None:
        """分析完成后的记忆处理。全部 fire-and-forget，不阻塞响应。"""
        # 1. 结构化记忆提取
        if self._settings.memory.long_term_enabled:
            asyncio.create_task(
                self._extract_memories(result),
                name=f"mem_extract_{result.trace_id}",
            )

        # 2. 短期记忆摘要
        if self._settings.memory.short_term_summary_enabled:
            asyncio.create_task(
                self._summarize_result(result, session_id),
                name=f"mem_summary_{session_id}",
            )

        # 3. 实体上下文保存
        asyncio.create_task(
            self._save_entities(session_id, parsed_params, result),
            name=f"mem_entities_{session_id}",
        )

        # 4. 整合触发检查
        if self._settings.memory.consolidation.enabled:
            asyncio.create_task(
                self._maybe_consolidate(result.user_id),
                name=f"mem_consolidate_{result.user_id}",
            )

    # ── 每轮：反馈检测 ─────────────────────────────────────

    def on_user_message(
        self,
        query: str,
        session_id: str,
        session_context: dict[str, Any],
    ) -> None:
        """检测用户消息中的反馈/纠正信号。fire-and-forget。"""
        if not self._settings.memory.feedback.enabled:
            return
        detector = self._get_feedback_detector()
        signal = detector.detect_signal(query)  # 同步预筛，<1ms
        if signal:
            asyncio.create_task(
                self._extract_feedback(query, session_context, signal),
                name=f"mem_feedback_{session_id}",
            )

    # ── 会话管理 ───────────────────────────────────────────

    def load_session(self, session_id: str) -> dict[str, Any]:
        """加载短期记忆上下文（对话历史 + 实体上下文）。"""
        return self._short_term.load_session_context(session_id)

    def save_session_entities(
        self, session_id: str, entities: dict[str, Any]
    ) -> None:
        """写入实体上下文到 session_entities 表。"""
        self._short_term.save_entity_context(session_id, entities)

    # ── 生命周期 ───────────────────────────────────────────

    def close(self) -> None:
        """关闭所有内部组件（进程退出时调用）。"""
        self._short_term.close()

    # ── 以下为内部方法（private），省略具体实现 ──────────────
    # _search_and_format, _extract_memories, _summarize_result,
    # _save_entities, _maybe_consolidate, _extract_feedback,
    # _get_feedback_detector, _get_extractor, _get_mem_repo
```

#### Orchestrator 集成（改造前 vs 改造后）

**改造前**（Orchestrator 直接依赖 6 个模块）：

```python
class Orchestrator:
    def __init__(self, settings, provider):
        self._short_term = ShortTermMemory(settings)        # 依赖 1
        # analyze() 中还会 import:
        # get_memory_repository()                            # 依赖 2
        # MemoryExtractor                                    # 依赖 3
        # FeedbackDetector                                   # 依赖 4
        # format_memory_injection                            # 依赖 5
        # run_consolidation                                  # 依赖 6
```

**改造后**（Orchestrator 只依赖 MemoryManager）：

```python
class Orchestrator:
    def __init__(self, settings, provider):
        from core.memory.manager import MemoryManager
        self._memory = MemoryManager(settings)              # 唯一依赖

    async def analyze(self, request):
        # 1. 加载短期记忆
        session_ctx = self._memory.load_session(request.session_id)

        # 2. 路由
        signal = self._intent_router.route(query, ...)

        # 3. 反馈检测（fire-and-forget）
        self._memory.on_user_message(query, session_id, session_ctx)

        # 4. 构建长期记忆上下文（同步，带超时）
        memory_context = await self._memory.build_context(
            user_id, query, parsed_params, analysis_type,
        )

        # 5. 执行分析（三条路径统一出口）
        if use_lookup_shortcut:
            result = await self._try_lookup_shortcut(...)   # Lookup 快捷路径
        elif use_dag:
            result = await self._execute_dag(...)            # DAG 路径
        else:
            result = await self._execute_react(...)          # ReAct 路径

        # 6. 分析后处理（fire-and-forget）——所有路径统一触发
        self._memory.on_analysis_complete(result, session_id, parsed_params)

        return result
```

**关键设计点——三条执行路径统一出口**：

Orchestrator 现有 3 条执行路径（DAG / ReAct / Lookup 快捷路径），`on_analysis_complete` 必须在所有路径的出口都被调用。Lookup 快捷路径虽然跳过了 ReAct，但其产出的 `AnalysisResult` 同样包含实体和查询结果，应触发记忆提取（entity_profile）和整合检查。

```
analyze()
  ├─ Lookup 快捷路径 → AnalysisResult ─┐
  ├─ DAG 路径         → AnalysisResult ─┼─→ on_analysis_complete()
  └─ ReAct 路径       → AnalysisResult ─┘
```

---

### A2. 现有系统对照表

| 组件 | 现状 | 目标 | 变更类型 |
|------|------|------|---------|
| **短期记忆 - Checkpointer** | LangGraph PostgresSaver，20 条滑动窗口，简单截断 | 保留，增强截断前实体保全 + 异步 LLM 摘要 | **改造** |
| **短期记忆 - Session Entities** | session_entities 表，跨轮实体上下文 | 保留不变 | **保留** |
| **短期记忆 - ReAct 裁剪** | MemoryMiddleware 截断早期 ToolMessage | 保留不变 | **保留** |
| **长期记忆 - MemoryRepository** | memories 表 + Chroma，flat memory_type，hybrid 检索 | 改造：引入 5 种结构化类型，增强写入管线 | **改造** |
| **长期记忆 - ReportRepository** | reports 表 + Chroma，审计资产 | 保留不变 | **保留** |
| **长期记忆 - 写入管线** | save_memory 直接写入，3 层过滤（长度/去重/空结论） | 改造：分析完成后结构化提取，5 种类型各自提取逻辑 | **改造** |
| **长期记忆 - 检索** | hybrid 召回（FTS + 向量 + RRF），无类型感知 | 增强：按类型优先级分配 token 预算 | **改造** |
| **长期记忆 - 注入** | search 结果直接拼入 prompt | 改造：结构化格式注入，按类型分区 | **改造** |
| **记忆整合** | 无 | **新增**：后台整合任务（合并/去重/修剪/TTL 淘汰） | **新增** |
| **记忆摘要** | 无 | **新增**：异步 LLM 摘要压缩分析结果 | **新增** |
| **可观测性** | record_memory_span 基础 span | 增强：整合/摘要/注入独立 span | **改造** |
| **配置** | memory.short_term / long_term / react | 扩展：新增 consolidation / summary / type_budget 配置块 | **改造** |

**不变更的组件：**
- `core/memory/trimmer.py`（MemoryMiddleware）—— 职责清晰，无需改动
- `core/memory/tables.py` 中的 `reports_table` / `session_entities_table` —— 结构不变
- `core/orchestrator/entity.py`（指代消解 + DB 验证 + 级联补充）—— 独立于记忆体系
- `LongTermMemory` facade 类 —— 保持向后兼容接口，内部委托给增强后的 MemoryRepository

---

## B. 记忆类型体系

### B3. 五种记忆类型完整定义

**封闭分类原则**：5 种类型是封闭集合，不允许运行时自由扩展。原因与参考文档一致——开放分类会导致 LLM 命名漂移（今天叫 `preference`、明天叫 `habit`），检索时无法稳定过滤。扩展类型需修改代码 + 配置。

```python
# core/memory/types.py（新增文件）
from enum import StrEnum

class MemoryType(StrEnum):
    USER_PREFERENCE = "user_preference"
    ENTITY_PROFILE = "entity_profile"
    ANALYSIS_INSIGHT = "analysis_insight"
    CORRECTION = "correction"
    DOMAIN_FACT = "domain_fact"
```

#### B3.1 user_preference — 用户分析偏好

| 维度 | 说明 |
|------|------|
| **记什么** | 用户的分析习惯、输出格式偏好、关注的指标阈值、常用查询参数 |
| **写入来源** | 用户显式表达（"我喜欢表格输出"）+ 从历史请求参数中归纳（连续 5 次用 30 天范围） |
| **写入时机** | 用户反馈 LLM 提取 / 整合阶段从历史请求归纳 |
| **TTL** | 不过期（用户偏好相对稳定） |
| **整合策略** | 同一偏好维度合并为最新值，冲突时以最近表达为准 |
| **检索权重** | 中等——每次分析都可能用到，但 token 占用小 |

**存储格式示例：**

```json
{
  "id": "mem_xxxx",
  "user_id": "user_001",
  "memory_type": "user_preference",
  "content": "用户偏好表格格式输出分析结果，关注价格差异率超过 5% 的订单，默认查询时间范围 30 天",
  "attrs": {
    "preferences": {
      "output_format": "table",
      "price_variance_threshold": 0.05,
      "default_time_range_days": 30
    },
    "source": "user_explicit",
    "confidence": 0.9
  }
}
```

#### B3.2 entity_profile — 业务实体累积画像

| 维度 | 说明 |
|------|------|
| **记什么** | 供应商/PO/发票等实体的历史表现、关键指标、风险标签 |
| **写入来源** | 分析结果中自动提取（三路匹配率、价格偏差、交付准时率等） |
| **写入时机** | 分析完成后的结构化提取（post-analyze hook） |
| **TTL** | 90 天（业务数据有时效性） |
| **整合策略** | 同一实体 ID 的多条记录合并为一条，保留最新指标 + 追加历史趋势 |
| **检索权重** | 最高——用户提到具体实体时优先召回 |

**存储格式示例：**

```json
{
  "id": "mem_xxxx",
  "user_id": "user_001",
  "memory_type": "entity_profile",
  "content": "供应商 SUP-003：近 30 天三路匹配率 88.5%，价格偏差率 8.2%（超阈值），涉及 5 笔 PO，主要供应电子元器件。风险标签：价格异常",
  "attrs": {
    "entity_type": "supplier",
    "entity_id": "SUP-003",
    "metrics": {
      "three_way_match_rate": 0.885,
      "price_variance_rate": 0.082,
      "po_count": 5,
      "category": "电子元器件"
    },
    "risk_tags": ["price_anomaly"],
    "analysis_date": "2026-04-19",
    "source_analysis_type": "three_way_match"
  }
}
```

#### B3.3 analysis_insight — 跨次分析趋势/模式

| 维度 | 说明 |
|------|------|
| **记什么** | 跨次分析发现的趋势、模式、周期性规律 |
| **写入来源** | 分析完成后自动归纳（对比本次结果与该用户的历史分析记忆） |
| **写入时机** | 分析完成后的结构化提取（需要与历史记忆对比才能判断是否为趋势） |
| **TTL** | 60 天（趋势有时效性，过旧的趋势可能已反转） |
| **整合策略** | 3 条以上同类发现合并为 1 条趋势摘要（LLM 参与归纳） |
| **检索权重** | 中低——只在用户查询涉及"趋势""变化""近期"等语义时召回 |

**存储格式示例：**

```json
{
  "id": "mem_xxxx",
  "user_id": "user_001",
  "memory_type": "analysis_insight",
  "content": "用户近 2 周连续 3 次查询付款合规分析，均涉及供应商 SUP-003。三路匹配异常数量呈上升趋势：4月5日 2 条 → 4月12日 4 条 → 4月19日 6 条",
  "attrs": {
    "pattern_type": "recurring_query",
    "related_entities": ["SUP-003"],
    "related_analysis_types": ["payment_compliance", "three_way_match"],
    "trend_direction": "worsening",
    "observation_count": 3,
    "first_observed": "2026-04-05",
    "last_observed": "2026-04-19"
  }
}
```

#### B3.4 correction — 用户对分析结果的修正

| 维度 | 说明 |
|------|------|
| **记什么** | 用户对分析结论的否定、补充、修正信息 |
| **写入来源** | 用户反馈时 LLM 提取（判断用户意图是纠正还是追问） |
| **写入时机** | 用户反馈 LLM 提取 |
| **TTL** | 180 天（修正信息保留较长，防止同类错误重复） |
| **整合策略** | 与 domain_fact 冲突检测；多次修正同一问题时保留最新 |
| **检索权重** | 高——相关实体/分析类型的修正必须优先注入，防止重复犯错 |

**存储格式示例：**

```json
{
  "id": "mem_xxxx",
  "user_id": "user_001",
  "memory_type": "correction",
  "content": "用户指出 PO-10045 的价格差异是因为批量折扣协议，不应标记为异常。该供应商 SUP-003 在大批量订单时享有 8% 折扣",
  "attrs": {
    "corrected_analysis_type": "price_variance",
    "related_entities": {"po_number": "PO-10045", "supplier_id": "SUP-003"},
    "correction_type": "false_positive",
    "rule_override": "price_variance > 5% 不适用于有批量折扣协议的订单",
    "source_trace_id": "trace_xxxx"
  }
}
```

#### B3.5 domain_fact — 业务领域事实知识

| 维度 | 说明 |
|------|------|
| **记什么** | 企业特定的业务规则、阈值、例外情况、组织结构等事实 |
| **写入来源** | 用户告知（"我们的三路匹配容差是 2%"）/ 从 correction 中提炼（多次修正同一规则 → 固化为 fact） |
| **写入时机** | 用户反馈 LLM 提取 / 整合阶段从 correction 提炼 |
| **TTL** | 不过期（业务规则相对稳定，需人工主动删除才失效） |
| **整合策略** | 与 correction 冲突检测（correction 可能推翻已有 fact）；同一规则合并 |
| **检索权重** | 高——涉及相关分析类型时必须注入，直接影响分析结论的正确性 |

**存储格式示例：**

```json
{
  "id": "mem_xxxx",
  "user_id": "user_001",
  "memory_type": "domain_fact",
  "content": "该企业三路匹配容差阈值为 2%（非系统默认的 5%）。SUP-002 是独家供应商，不适用供应商比价分析",
  "attrs": {
    "fact_category": "business_rule",
    "scope": "enterprise",
    "rules": [
      {"rule_type": "threshold_override", "target": "three_way_match_tolerance", "value": 0.02},
      {"rule_type": "entity_exception", "target": "supplier_comparison", "entity_id": "SUP-002", "reason": "sole_supplier"}
    ]
  }
}
```

#### B3.6 五类对比速查表

| 类型 | 写入触发 | TTL | 整合策略 | token 预算占比 | 检索优先级 |
|------|---------|-----|---------|--------------|-----------|
| user_preference | 用户反馈 / 整合归纳 | 不过期 | 同维度合并 | 20% | 中 |
| entity_profile | 分析完成后提取 | 90 天 | 同实体 ID 合并 | 40% | 最高 |
| analysis_insight | 分析完成后归纳 | 60 天 | 同类发现合并 | 15% | 中低 |
| correction | 用户反馈 LLM 提取 | 180 天 | 与 domain_fact 冲突检测 | 15% | 高 |
| domain_fact | 用户告知 / 从 correction 提炼 | 不过期 | 同规则合并 | 10% | 高 |

---

### B4. memories 表 Schema 变更设计

#### B4.1 现有 memories 表结构

```sql
-- core/memory/tables.py 当前定义
CREATE TABLE memories (
    id              VARCHAR(36)  PRIMARY KEY,
    user_id         VARCHAR(128) NOT NULL,       -- 索引
    session_id      VARCHAR(128) NOT NULL,       -- 索引
    memory_type     VARCHAR(64)  NOT NULL,       -- 当前无约束，自由文本
    content         TEXT         NOT NULL,
    content_hash    VARCHAR(16),                 -- 去重指纹
    attrs           JSON,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
```

#### B4.2 变更方案

**原则**：在现有表上增量变更，不拆表。通过新增列 + 索引满足类型体系需求。

```sql
-- 新增列
ALTER TABLE memories ADD COLUMN entity_id      VARCHAR(128);   -- entity_profile 专用：关联的实体 ID
ALTER TABLE memories ADD COLUMN expires_at      TIMESTAMPTZ;    -- TTL 到期时间（NULL = 不过期）
ALTER TABLE memories ADD COLUMN consolidated_at TIMESTAMPTZ;    -- 最近一次被整合的时间（NULL = 从未整合）
ALTER TABLE memories ADD COLUMN is_consolidated BOOLEAN NOT NULL DEFAULT false;  -- 整合产出的记忆标记
ALTER TABLE memories ADD COLUMN source_ids     JSON;           -- 整合时，来源记忆的 ID 列表

-- 新增索引
CREATE INDEX memories_type_user     ON memories (memory_type, user_id);           -- 按类型查用户记忆
CREATE INDEX memories_entity        ON memories (user_id, entity_id)
    WHERE entity_id IS NOT NULL;                                                  -- entity_profile 按实体 ID 检索
CREATE INDEX memories_expires       ON memories (expires_at)
    WHERE expires_at IS NOT NULL;                                                 -- TTL 淘汰扫描
CREATE INDEX memories_consolidation ON memories (user_id, is_consolidated, memory_type);  -- 整合候选查询

-- memory_type 值约束（应用层枚举校验，不用 DB CHECK 约束——方便未来扩展）
-- 有效值：user_preference / entity_profile / analysis_insight / correction / domain_fact
```

#### B4.3 新增表：memory_consolidation_log

记录每次整合的执行状态，用于触发条件判定 + 审计追溯。

```sql
CREATE TABLE memory_consolidation_log (
    id              VARCHAR(36)  PRIMARY KEY,
    user_id         VARCHAR(128) NOT NULL,
    started_at      TIMESTAMPTZ  NOT NULL,
    completed_at    TIMESTAMPTZ,                 -- NULL = 进行中或失败
    status          VARCHAR(16)  NOT NULL,        -- running / completed / failed
    input_count     INTEGER      NOT NULL DEFAULT 0,   -- 整合前记忆条数
    merged_count    INTEGER      NOT NULL DEFAULT 0,   -- 合并产出的记忆条数
    pruned_count    INTEGER      NOT NULL DEFAULT 0,   -- TTL 淘汰的条数
    llm_used        BOOLEAN      NOT NULL DEFAULT false, -- 是否使用了 LLM 合并
    error_message   TEXT,                        -- 失败时的错误信息
    details         JSON                         -- 整合细节（各类型的操作明细）
);

CREATE INDEX consolidation_user_status ON memory_consolidation_log (user_id, status);
CREATE INDEX consolidation_completed   ON memory_consolidation_log (user_id, completed_at DESC);
```

#### B4.4 Alembic 迁移计划

一次迁移脚本完成所有变更（原子性）：

```python
# migrations/versions/0010_memory_type_system.py
"""
Add memory type system: new columns, indexes, and consolidation log table.
"""

def upgrade():
    # 1. memories 表新增列
    op.add_column('memories', sa.Column('entity_id', sa.String(128), nullable=True))
    op.add_column('memories', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('memories', sa.Column('consolidated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('memories', sa.Column('is_consolidated', sa.Boolean, nullable=False, server_default='false'))
    op.add_column('memories', sa.Column('source_ids', sa.JSON, nullable=True))

    # 2. 新增索引
    op.create_index('memories_type_user', 'memories', ['memory_type', 'user_id'])
    op.create_index('memories_entity', 'memories', ['user_id', 'entity_id'],
                    postgresql_where=sa.text("entity_id IS NOT NULL"))
    op.create_index('memories_expires', 'memories', ['expires_at'],
                    postgresql_where=sa.text("expires_at IS NOT NULL"))
    op.create_index('memories_consolidation', 'memories',
                    ['user_id', 'is_consolidated', 'memory_type'])

    # 3. 创建 memory_consolidation_log 表
    op.create_table('memory_consolidation_log', ...)

    # 4. 历史数据兼容：已有记忆的 memory_type 保持原值，不强制迁移
    #    新写入走 MemoryType 枚举校验；旧数据在整合时自然归类或标记为 legacy

def downgrade():
    op.drop_table('memory_consolidation_log')
    op.drop_index('memories_consolidation')
    op.drop_index('memories_expires')
    op.drop_index('memories_entity')
    op.drop_index('memories_type_user')
    op.drop_column('memories', 'source_ids')
    op.drop_column('memories', 'is_consolidated')
    op.drop_column('memories', 'consolidated_at')
    op.drop_column('memories', 'expires_at')
    op.drop_column('memories', 'entity_id')
```

#### B4.5 历史数据兼容策略

| 场景 | 处理 |
|------|------|
| 现有记忆的 memory_type 不在 5 种枚举中 | 检索时照常返回，不过滤。写入时校验枚举 |
| 现有记忆无 entity_id / expires_at | NULL 值，不影响查询。整合时可按需补充 |
| 整合首次运行 | 对存量记忆做一次全量分类（可选，通过管理 API 手动触发） |

---

## C. 短期记忆改造

### C5. LLM 异步摘要压缩方案

#### C5.1 问题背景

ERP 分析结果的信息分布是倒金字塔——原始数据表在前，关键结论在后。简单截断（`trim_to_token_budget`）保留前 N 字符，丢弃的恰恰是结论和异常汇总。

```
分析结果（约 3000 字）
├─ [前 500 字]    查询条件 + 数据表头       ← 截断保留（低价值）
├─ [500~2000 字]  50 条匹配明细表           ← 截断丢弃
├─ [2000~2500 字] 异常汇总 + 风险评级       ← 截断丢弃（高价值）
└─ [2500~3000 字] 结论建议                  ← 截断丢弃（高价值）

LLM 摘要（约 300 字）
├─ 分析类型 + 覆盖范围
├─ 关键发现（Top-N 异常）
├─ 风险评级
└─ 结论建议
```

#### C5.2 整体流程

```
分析完成，结果返回用户
  │
  ├─ 同步：完整结果写入 checkpointer（作为 AIMessage）
  │        同时做截断兜底（trim_to_token_budget）
  │
  └─ 异步：if summary_enabled AND len(result) > summary_max_input_chars:
             │
             ├─ 调用 llm_fast 生成结构化摘要
             │    ├─ 成功 → 用摘要替换 checkpointer 中的 AIMessage
             │    │         记录 memory.summary span（status=ok）
             │    └─ 失败 → 保留截断版本
             │              记录 memory.summary span（status=error）
             │              WARNING 日志
             │
             └─ 用户下次追问时，读到的是摘要版本（或截断兜底）
```

#### C5.3 摘要 Prompt 设计

```python
SUMMARY_SYSTEM_PROMPT = """你是 ERP 采购分析结果的摘要生成器。
将分析结果压缩为结构化摘要，保留所有关键信息，丢弃原始明细数据。

输出格式（严格遵循）：
## 分析摘要
- **分析类型**：{type}
- **覆盖范围**：时间范围、涉及实体数量
- **关键发现**：列出 Top-5 异常/发现，每条一行
- **风险评级**：高/中/低
- **结论建议**：1-2 句话

要求：
1. 不超过 500 tokens
2. 保留所有实体 ID（PO 号、供应商 ID、发票号等）
3. 保留所有数值指标（匹配率、偏差率等）
4. 丢弃原始数据表的逐行明细
5. 不要添加分析结果中没有的信息
"""
```

#### C5.4 竞态处理

```
t=0   分析完成 → 返回用户 + 同步写 checkpointer（完整/截断版）
t=1   异步摘要任务启动
t=2   用户追问 → 读 checkpointer → 读到截断版（摘要未完成）→ 可接受
t=3   摘要完成 → 替换 checkpointer 中的 AIMessage
t=4   用户再次追问 → 读到摘要版 → 最优体验
```

**关键点**：t=2 时用户读到的是截断版本，不是空的——语义不完美但不中断对话。这是"最终一致"模型，对 ERP 分析场景可接受。

#### C5.5 实现位置

```
core/memory/short_term.py
  └─ ShortTermMemory
       └─ async summarize_and_replace(session_id, ai_message, llm_fast)  ← 新增方法

core/orchestrator/orchestrator.py
  └─ _post_analyze()  ← 现有流程末尾追加异步摘要触发
```

#### C5.6 配置项

```yaml
memory:
  short_term:
    # ... 现有配置保留 ...
    summary_enabled: true             # LLM 摘要总开关
    summary_max_input_chars: 5000     # 超过此长度才触发摘要（短结果无需摘要）
    summary_max_output_tokens: 500    # 摘要输出 token 上限
    summary_timeout_seconds: 30       # LLM 调用超时
```

---

### C6. 滑动窗口截断增强

#### C6.1 现有问题

当前 `_truncate_checkpointer_history` 简单丢弃最早的消息。如果被丢弃的消息中包含实体引用（"分析一下 PO-10045"），后续的指代消解会失去上下文。

session_entities 表已经独立存储实体上下文，但截断前没有主动从即将丢弃的消息中提取实体。

#### C6.2 增强方案

```
截断前（滑动窗口即将丢弃消息 M1~M5）：
  │
  ├─ 1. 从 M1~M5 中提取实体（复用 _extract_params 正则）
  ├─ 2. 与 session_entities 已有值 merge（新值不覆盖已有值——已有值更可靠）
  └─ 3. 写入 session_entities 表
  │
  然后执行截断
```

**改动范围**：仅修改 `modules/p2p/agent.py` 的 `_truncate_checkpointer_history` 方法，在截断前增加一步实体提取。改动量小（约 20 行），且复用现有的 `_extract_params` 函数。

#### C6.3 代码示意

```python
# modules/p2p/agent.py — _truncate_checkpointer_history 增强
def _truncate_checkpointer_history(self, session_id: str, max_messages: int) -> None:
    # ... 现有逻辑：读取 checkpoint，判断是否需要截断 ...

    if len(messages) <= max_messages:
        return

    # 新增：从即将丢弃的消息中提取实体
    to_discard = messages[:len(messages) - max_messages]
    extracted_entities = {}
    for msg in to_discard:
        content = getattr(msg, "content", "")
        if content and isinstance(content, str):
            params = _extract_params(content, self._settings.analysis.entity_patterns)
            for k, v in params.items():
                if k != "days" and v and k not in extracted_entities:
                    extracted_entities[k] = v

    # 保全到 session_entities（不覆盖已有值）
    if extracted_entities:
        self._short_term.save_entity_context(session_id, extracted_entities)

    # 现有截断逻辑继续 ...
```

---

## D. 长期记忆增强

### D7. 写入管线设计

#### D7.1 写入触发点

与参考文档（每轮末 fork agent 提取）不同，ERP agent 在**分析完成后**做一次结构化记忆提取。原因：ERP 分析是请求-响应模式，单次分析信息密度高，对话轮次少。

```
POST /analyze (或 /analyze/async)
  │
  ├─ 路由 → DAG/ReAct 执行 → AnalysisResult 产出
  │
  └─ _post_analyze() hook（现有流程）
       ├─ _persist_report()            ← 现有：写报告到 reports 表
       ├─ _save_session_entities()     ← 现有：写实体到 session_entities
       ├─ save_dag_result()            ← 现有：写结果到 checkpointer
       │
       └─ extract_memories()           ← 新增：结构化记忆提取
            ├─ extract_entity_profile()      → entity_profile
            ├─ extract_analysis_insight()    → analysis_insight
            └─ (domain_fact 不在此处写入——来源于用户告知或整合提炼)
```

#### D7.2 提取管线：MemoryExtractor

新增 `core/memory/extractor.py`，职责单一：从 AnalysisResult 中提取结构化记忆。

```python
class MemoryExtractor:
    """从分析结果中提取结构化记忆，写入长期记忆。

    设计要点：
    - 纯函数式提取，不持有状态
    - 每种类型有独立的提取方法，互不影响
    - 单条提取失败不影响其他类型
    - 通过 MemoryRepository.save() 复用已有的去重/cap/向量写入逻辑
    """

    def __init__(self, repo: MemoryRepository, settings: Settings) -> None:
        self._repo = repo
        self._settings = settings

    async def extract(
        self,
        result: AnalysisResult,
        user_id: str,
        session_id: str,
    ) -> dict[str, str | None]:
        """从分析结果提取所有类型的记忆，返回各类型写入的 memory_id。"""
        outcomes = {}

        # 1. entity_profile — 从结果中提取实体画像
        outcomes["entity_profile"] = await self._extract_entity_profile(
            result, user_id, session_id
        )

        # 2. analysis_insight — 对比历史，发现趋势
        outcomes["analysis_insight"] = await self._extract_analysis_insight(
            result, user_id, session_id
        )

        return outcomes
```

#### D7.3 entity_profile 提取逻辑

```python
async def _extract_entity_profile(
    self, result: AnalysisResult, user_id: str, session_id: str
) -> str | None:
    """从分析结果中提取实体画像。

    提取规则：
    - 分析结果中出现的 supplier_id → 提取该供应商的指标
    - 指标来源：result.anomalies 中的数值 + report_markdown 中的汇总
    - 如果该实体已有 entity_profile → 写入新条目（整合时合并）
    """
    # 从 result 中收集涉及的实体
    entities = self._collect_entities(result)
    if not entities:
        return None

    # 为每个实体生成画像记忆
    memory_id = None
    for entity_type, entity_id in entities:
        content = self._build_entity_content(result, entity_type, entity_id)
        attrs = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "source_analysis_type": result.analysis_type.value,
            "analysis_date": result.created_at.isoformat() if result.created_at else None,
            "anomaly_count": len([
                a for a in result.anomalies
                if entity_id in str(a)
            ]),
        }
        # 计算 TTL
        expires_at = now_cn() + timedelta(days=self._settings.memory.ttl.entity_profile_days)

        mid = self._repo.save(
            user_id=user_id,
            session_id=session_id,
            memory_type=MemoryType.ENTITY_PROFILE,
            content=content,
            metadata=attrs,
            entity_id=entity_id,
            expires_at=expires_at,
        )
        if mid:
            memory_id = mid

    return memory_id
```

#### D7.4 analysis_insight 提取逻辑

```python
async def _extract_analysis_insight(
    self, result: AnalysisResult, user_id: str, session_id: str
) -> str | None:
    """对比历史分析记忆，发现趋势/模式。

    规则：
    - 查询该用户最近 N 条同类型分析的 entity_profile
    - 对比关键指标变化（三路匹配率上升/下降、异常数量趋势等）
    - 只在有明确趋势时才写入（避免噪声）
    """
    # 查询历史同类分析的记忆
    recent = self._repo.search(
        user_id=user_id,
        query=result.analysis_type.value,
        limit=10,
    )
    same_type = [
        m for m in recent
        if m.get("attrs", {}).get("source_analysis_type") == result.analysis_type.value
    ]

    if len(same_type) < 2:
        # 不足 2 条历史，无法判断趋势
        return None

    # 构建趋势描述
    trend = self._detect_trend(same_type, result)
    if not trend:
        return None

    expires_at = now_cn() + timedelta(days=self._settings.memory.ttl.analysis_insight_days)

    return self._repo.save(
        user_id=user_id,
        session_id=session_id,
        memory_type=MemoryType.ANALYSIS_INSIGHT,
        content=trend["description"],
        metadata={
            "pattern_type": trend["type"],
            "related_entities": trend.get("entities", []),
            "related_analysis_types": [result.analysis_type.value],
            "trend_direction": trend.get("direction"),
            "observation_count": len(same_type) + 1,
        },
        expires_at=expires_at,
    )
```

#### D7.5 写入管线的集成点

```python
# core/orchestrator/orchestrator.py — _post_analyze() 新增

async def _post_analyze(self, result: AnalysisResult, ...) -> None:
    """分析完成后的 hook：报告持久化 + 记忆提取。"""
    # 现有逻辑
    self._persist_report(result)
    await self._save_session_entities(session_id, parsed_params, result)

    # 新增：结构化记忆提取（fire-and-forget，不阻塞响应返回）
    if self._settings.memory.long_term_enabled:
        asyncio.create_task(
            self._extract_memories_async(result),
            name=f"memory_extract_{result.trace_id}",
        )


async def _extract_memories_async(self, result: AnalysisResult) -> None:
    """异步记忆提取，fire-and-forget。所有异常内部捕获，不抛出。"""
    try:
        from core.memory.extractor import MemoryExtractor
        from core.memory.long_term import get_memory_repository

        extractor = MemoryExtractor(
            repo=get_memory_repository(),
            settings=self._settings,
        )
        outcomes = await extractor.extract(
            result=result,
            user_id=result.user_id,
            session_id=result.session_id,
        )
        _logger.info("memory extraction completed: %s", outcomes)
    except Exception as exc:
        _logger.warning("memory extraction failed (non-blocking): %s", exc)
```

---

### D8. 用户反馈/纠正的 LLM 提取方案

#### D8.1 问题场景

用户在分析结果返回后的追问中，可能包含三类意图：

| 意图 | 示例 | 处理 |
|------|------|------|
| **追问**（follow-up） | "再看看这个供应商的付款情况" | 正常路由到新分析，不触发记忆提取 |
| **纠正**（correction） | "这个价格差异是因为批量折扣，不算异常" | 提取为 correction 类型记忆 |
| **偏好表达** | "以后都用表格输出" / "我只关心超过 5% 的差异" | 提取为 user_preference 类型记忆 |
| **事实告知** | "我们公司的匹配容差是 2%" | 提取为 domain_fact 类型记忆 |

**核心挑战**：需要 LLM 判断用户意图属于哪一类，并提取结构化记忆内容。

#### D8.2 判定流程

```
用户输入 query
  │
  ├─ IntentRouter 路由（现有流程）
  │    ├─ META / CHITCHAT / CLARIFICATION → 模板响应，不提取
  │    ├─ 分析意图 → 正常分析流程
  │    └─ OUT_OF_SCOPE → 拒绝，不提取
  │
  └─ 并行：FeedbackDetector.detect(query, session_context)  ← 新增
       │
       ├─ 无反馈信号 → 跳过
       └─ 检测到反馈 → 调用 llm_fast 提取
            ├─ correction → save(memory_type=CORRECTION)
            ├─ user_preference → save(memory_type=USER_PREFERENCE)
            └─ domain_fact → save(memory_type=DOMAIN_FACT)
```

#### D8.3 FeedbackDetector 设计

```python
# core/memory/feedback.py（新增文件）

class FeedbackDetector:
    """检测用户输入中的反馈/纠正/偏好信号。

    两阶段设计：
    1. 关键词预筛（零成本）——快速排除明显的分析请求
    2. LLM 精确分类（仅在预筛通过时调用）
    """

    # 预筛关键词：覆盖纠正、偏好、事实告知的常见表达
    _CORRECTION_SIGNALS = {
        "不对", "不是", "错了", "不算", "不应该", "其实是", "实际上",
        "是因为", "原因是", "不要标记", "误报", "不准确",
    }
    _PREFERENCE_SIGNALS = {
        "以后", "默认", "总是", "每次", "偏好", "喜欢", "习惯",
        "用表格", "用图表", "只看", "只关心", "关注",
    }
    _FACT_SIGNALS = {
        "我们公司", "我们的", "企业规定", "标准是", "阈值是",
        "容差", "规则是", "政策是", "独家供应商",
    }

    def detect_signal(self, query: str) -> str | None:
        """关键词预筛：返回 'correction' / 'preference' / 'fact' / None。"""
        q = query.lower()
        if any(w in q for w in self._CORRECTION_SIGNALS):
            return "correction"
        if any(w in q for w in self._PREFERENCE_SIGNALS):
            return "preference"
        if any(w in q for w in self._FACT_SIGNALS):
            return "fact"
        return None

    async def extract(
        self,
        query: str,
        session_context: dict,
        signal_type: str,
        llm_fast: BaseChatModel,
    ) -> dict | None:
        """LLM 精确提取反馈内容。仅在 detect_signal 命中时调用。"""
        prompt = self._build_extract_prompt(query, session_context, signal_type)
        response = await llm_fast.ainvoke(prompt)
        return self._parse_response(response, signal_type)
```

#### D8.4 LLM 提取 Prompt

```python
FEEDBACK_EXTRACT_PROMPT = """你是 ERP 采购分析系统的反馈提取器。
用户刚才收到了一份分析报告，现在发送了新的消息。

用户消息：{query}
最近分析摘要：{context_summary}

请判断用户消息是否包含以下任一类型的反馈：

1. correction — 对分析结果的修正（指出错误、补充信息）
2. user_preference — 分析偏好表达（输出格式、关注阈值等）
3. domain_fact — 业务事实告知（企业规则、阈值、例外情况）
4. none — 不是反馈，是追问或新的分析请求

输出 JSON 格式：
{
  "type": "correction|user_preference|domain_fact|none",
  "content": "提取的记忆内容（自然语言描述）",
  "related_entities": {"po_number": "...", "supplier_id": "..."},
  "confidence": 0.0~1.0
}

如果 type=none 或 confidence<0.6，content 为空字符串。
"""
```

#### D8.5 集成点与性能考量

```
orchestrator.analyze()
  ├─ 路由 + 分析（主流程，不受影响）
  └─ 并行：feedback_detector.detect_signal(query)    ← 同步预筛，<1ms
       ├─ None → 跳过（零成本，大多数请求走这条路）
       └─ 有信号 → fire-and-forget LLM 提取（不阻塞主流程）
```

```python
# core/orchestrator/orchestrator.py — analyze() 中

signal = feedback_detector.detect_signal(query)  # 同步预筛，<1ms
if signal:
    asyncio.create_task(
        self._extract_feedback_async(query, session_context, signal),
        name=f"feedback_extract_{session_id}",
    )
    # 不 await，主流程继续路由 + 分析


async def _extract_feedback_async(self, query, session_context, signal) -> None:
    """异步反馈提取，fire-and-forget。"""
    try:
        result = await feedback_detector.extract(query, session_context, signal, llm_fast)
        if result and result.get("confidence", 0) >= self._settings.memory.feedback.min_confidence:
            repo = get_memory_repository()
            repo.save(...)
            _logger.info("feedback memory saved: type=%s", result["type"])
    except Exception as exc:
        _logger.warning("feedback extraction failed (non-blocking): %s", exc)
```

**性能影响**：关键词预筛是纯字符串匹配，耗时 <1ms。只有命中预筛的请求才调用 llm_fast（预计 <20% 的请求），且通过 `asyncio.create_task` fire-and-forget 执行，不阻塞分析主流程。

---

### D9. 检索与注入策略

#### D9.1 精确检索 vs 语义检索分析

ERP 场景下，不同记忆类型对检索精确性的要求差异很大。向量语义检索存在"近似但不正确"的风险——在 ERP 业务中，把 SUP-003 的修正应用到 SUP-004 比不召回更危险。

**各类型检索策略：**

| 类型 | 精确检索维度 | 语义检索 | 理由 |
|------|------------|---------|------|
| **entity_profile** | entity_id（必须） | 仅作补充 | 错误实体画像 → 引用错误数据 |
| **correction** | entity_id + analysis_type（必须） | 仅作补充 | 错召比漏召更危险 |
| **domain_fact** | analysis_type（必须）+ entity_id（有则匹配） | 仅作补充 | 规则应用错对象 → 错误结论 |
| **user_preference** | user_id（全量加载） | 不需要 | 全局偏好，无需语义匹配 |
| **analysis_insight** | 不需要 | 主检索方式 | 趋势天然模糊，语义匹配合适 |

**设计原则**：前三类采用"精确优先 + 语义补充"双通道，后两类单通道。

#### D9.2 双通道检索流程

```
用户查询 "分析 SUP-003 的三路匹配情况"
  │
  ├─ 路由阶段已解析：entity_ids=["SUP-003"], analysis_type="three_way_match"
  │
  ├─ 按类型分桶检索（并行）：
  │
  │  entity_profile（精确优先）:
  │    ├─ 通道 A：WHERE entity_id IN ('SUP-003')                → 精确命中
  │    └─ 通道 B：hybrid(query) WHERE memory_type='entity_profile' → 语义补充（A 为空时）
  │    合并：A 优先，B 去重补充至上限
  │
  │  correction（精确优先）:
  │    ├─ 通道 A：WHERE attrs->'related_entities' 包含 'SUP-003'  → 按实体精确
  │    ├─ 通道 A'：WHERE attrs->>'corrected_analysis_type' = 'three_way_match' → 按分析类型精确
  │    └─ 通道 B：hybrid(query) WHERE memory_type='correction'   → 语义补充
  │    合并：A∪A' 去重优先，B 去重补充至上限
  │
  │  domain_fact（精确优先）:
  │    ├─ 通道 A：WHERE attrs->'rules' 中 target 匹配 'three_way_match' → 按分析类型精确
  │    ├─ 通道 A'：WHERE entity_id = 'SUP-003'                  → 按实体精确（实体级规则）
  │    └─ 通道 B：hybrid(query) WHERE memory_type='domain_fact'  → 语义补充
  │    合并：A∪A' 去重优先，B 去重补充至上限
  │
  │  user_preference（全量加载）:
  │    └─ WHERE user_id=? AND memory_type='user_preference' ORDER BY created_at DESC LIMIT 3
  │
  │  analysis_insight（纯语义）:
  │    └─ hybrid(query) WHERE memory_type='analysis_insight'
  │
  ├─ 按类型 token 预算截断：
  │    ├─ entity_profile:   ≤40% of long_term budget (≈1536 tok)
  │    ├─ correction:       ≤15% (≈576 tok)
  │    ├─ domain_fact:      ≤10% (≈384 tok)
  │    ├─ user_preference:  ≤20% (≈768 tok)
  │    └─ analysis_insight: ≤15% (≈576 tok)
  │
  └─ 组装为结构化 prompt 注入
```

#### D9.3 新增方法：search_by_type

```python
# core/memory/long_term.py — MemoryRepository 新增方法

def search_by_type(
    self,
    user_id: str,
    query: str,
    entity_ids: list[str] | None = None,
    analysis_type: str | None = None,
    type_limits: dict[str, int] | None = None,
) -> dict[str, list[dict]]:
    """按类型分桶检索，精确优先 + 语义补充。

    Args:
        user_id: 用户 ID
        query: 检索查询（语义通道用）
        entity_ids: 精确匹配的实体 ID（entity_profile/correction/domain_fact 用）
        analysis_type: 精确匹配的分析类型（correction/domain_fact 用）
        type_limits: 各类型的最大返回条数

    Returns:
        按 memory_type 分组的记忆字典
    """
    limits = type_limits or {
        "entity_profile": 3, "correction": 2,
        "domain_fact": 3, "user_preference": 2, "analysis_insight": 2,
    }
    results = {}

    # entity_profile: 精确 entity_id → 语义补充
    results["entity_profile"] = self._dual_channel_search(
        user_id, query, "entity_profile", limits["entity_profile"],
        exact_entity_ids=entity_ids,
    )

    # correction: 精确 entity_id + analysis_type → 语义补充
    results["correction"] = self._dual_channel_search(
        user_id, query, "correction", limits["correction"],
        exact_entity_ids=entity_ids,
        exact_analysis_type=analysis_type,
    )

    # domain_fact: 精确 analysis_type + entity_id → 语义补充
    results["domain_fact"] = self._dual_channel_search(
        user_id, query, "domain_fact", limits["domain_fact"],
        exact_entity_ids=entity_ids,
        exact_analysis_type=analysis_type,
    )

    # user_preference: 全量加载
    results["user_preference"] = self._fetch_latest_by_type(
        user_id, "user_preference", limits["user_preference"],
    )

    # analysis_insight: 纯语义
    results["analysis_insight"] = self._search_by_type_hybrid(
        user_id, query, "analysis_insight", limits["analysis_insight"],
    )

    return results


def _dual_channel_search(
    self,
    user_id: str,
    query: str,
    memory_type: str,
    limit: int,
    exact_entity_ids: list[str] | None = None,
    exact_analysis_type: str | None = None,
) -> list[dict]:
    """双通道检索：精确优先，语义补充去重。"""
    exact_results = []

    # 通道 A：按 entity_id 精确匹配
    if exact_entity_ids:
        exact_results.extend(
            self._fetch_by_entity_and_type(user_id, exact_entity_ids, memory_type)
        )

    # 通道 A'：按 analysis_type 精确匹配（correction/domain_fact）
    if exact_analysis_type:
        exact_results.extend(
            self._fetch_by_analysis_type(user_id, exact_analysis_type, memory_type)
        )

    # 去重
    seen_ids = {r["id"] for r in exact_results}
    exact_results = [r for i, r in enumerate(exact_results)
                     if r["id"] not in {rr["id"] for rr in exact_results[:i]}]

    # 精确结果已达上限 → 不走语义通道
    if len(exact_results) >= limit:
        return exact_results[:limit]

    # 通道 B：语义补充
    remaining = limit - len(exact_results)
    semantic_results = self._search_by_type_hybrid(
        user_id, query, memory_type, remaining * 2,  # 多取一些以备去重
    )
    for r in semantic_results:
        if r["id"] not in seen_ids and len(exact_results) < limit:
            exact_results.append(r)
            seen_ids.add(r["id"])

    return exact_results
```

#### D9.3 Prompt 注入格式

```python
# core/memory/injection.py（新增文件）

def format_memory_injection(
    memories: dict[str, list[dict]],
    type_budgets: dict[str, int],
) -> str:
    """将分桶检索结果格式化为 LLM prompt 注入文本。

    格式设计原则：
    - 按类型分区，LLM 能清楚区分不同来源
    - correction 和 domain_fact 标注为"必须遵循"
    - entity_profile 提供历史上下文
    - user_preference 指导输出格式
    """
    sections = []

    # correction — 最高优先级，标注为规则
    if memories.get("correction"):
        lines = ["[历史修正记录 — 分析时必须遵循]"]
        for m in memories["correction"]:
            lines.append(f"- {m['content']}")
        sections.append("\n".join(lines))

    # domain_fact — 业务规则
    if memories.get("domain_fact"):
        lines = ["[业务规则 — 分析时必须遵循]"]
        for m in memories["domain_fact"]:
            lines.append(f"- {m['content']}")
        sections.append("\n".join(lines))

    # entity_profile — 实体历史画像
    if memories.get("entity_profile"):
        lines = ["[相关实体历史画像]"]
        for m in memories["entity_profile"]:
            lines.append(f"- {m['content']}")
        sections.append("\n".join(lines))

    # analysis_insight — 趋势参考
    if memories.get("analysis_insight"):
        lines = ["[历史趋势参考]"]
        for m in memories["analysis_insight"]:
            lines.append(f"- {m['content']}")
        sections.append("\n".join(lines))

    # user_preference — 输出偏好
    if memories.get("user_preference"):
        lines = ["[用户偏好]"]
        for m in memories["user_preference"]:
            lines.append(f"- {m['content']}")
        sections.append("\n".join(lines))

    if not sections:
        return ""

    return "\n\n".join(sections)
```

#### D9.4 注入点：Orchestrator 分析前（带超时保护）

检索注入必须同步完成（结果要注入分析 prompt），但通过超时保护确保不拖慢主流程。

```python
# core/orchestrator/orchestrator.py — analyze() 流程中

async def _build_memory_context(
    self, user_id: str, query: str, parsed_params: dict,
    analysis_type: str | None = None,
) -> str:
    """检索并构建长期记忆上下文，注入到分析 prompt 中。

    性能保护：
    - 精确查询走索引，单次 <5ms
    - 语义通道在精确结果已满时跳过
    - 整体超时保护（默认 200ms），超时返回空记忆
    """
    if not self._settings.memory.long_term_enabled:
        return ""

    try:
        return await asyncio.wait_for(
            self._search_and_format(user_id, query, parsed_params, analysis_type),
            timeout=self._settings.memory.long_term_search_timeout_seconds,
        )
    except asyncio.TimeoutError:
        _inc("ltm.search.timeout")
        _logger.warning(
            "memory search timeout (%.1fs), proceeding without memory context",
            self._settings.memory.long_term_search_timeout_seconds,
        )
        return ""
    except Exception as exc:
        _logger.warning("memory search failed (non-blocking): %s", exc)
        return ""


async def _search_and_format(
    self, user_id: str, query: str, parsed_params: dict,
    analysis_type: str | None,
) -> str:
    entity_ids = [
        v for k, v in parsed_params.items()
        if k in ("supplier_id", "po_number", "invoice_number") and v
    ]

    from core.memory.long_term import get_memory_repository
    from core.memory.injection import format_memory_injection

    repo = get_memory_repository()
    memories = await asyncio.to_thread(
        repo.search_by_type,
        user_id=user_id,
        query=query,
        entity_ids=entity_ids or None,
        analysis_type=analysis_type,
    )

    return format_memory_injection(
        memories,
        type_budgets=self._settings.memory.long_term_type_budget_pct,
    )
```

---

## E. 记忆整合（Consolidation）

### E10. 整合触发机制

#### E10a. 触发条件判定逻辑

双条件取先到者，均可配置：

```
每次分析完成（_post_analyze 末尾）
  │
  └─ should_consolidate(user_id) ?
       │
       ├─ 条件 A：该用户自上次整合以来新增记忆 ≥ consolidation_min_new_memories（默认 20）
       │          查询：SELECT COUNT(*) FROM memories
       │                WHERE user_id=? AND created_at > last_consolidation_completed_at
       │
       ├─ 条件 B：距上次整合完成 ≥ consolidation_max_interval_hours（默认 72h）
       │          查询：SELECT MAX(completed_at) FROM memory_consolidation_log
       │                WHERE user_id=? AND status='completed'
       │
       └─ A OR B 为 true → 提交异步整合任务
          否则 → 跳过
```

**性能考量**：两条 SQL 查询均有索引覆盖（`memories_user_created` + `consolidation_user_status`），耗时 <5ms。每次分析完成时执行一次，频率低。

#### E10b. per-user 整合锁设计

防止同一用户的整合任务并发执行。使用 `memory_consolidation_log` 表的 `status='running'` 行作为逻辑锁。

```python
async def try_acquire_consolidation_lock(user_id: str) -> str | None:
    """尝试获取整合锁，返回 log_id 或 None。

    锁语义：该用户在 consolidation_log 中有 status='running' 的行 → 锁被持有。
    超时保护：running 状态超过 consolidation_lock_timeout_seconds（默认 600s）→ 视为死锁，强制回收。
    """
    with engine.begin() as conn:
        # 检查是否有进行中的整合
        running = conn.execute(
            select(memory_consolidation_log.c.id, memory_consolidation_log.c.started_at)
            .where(and_(
                memory_consolidation_log.c.user_id == user_id,
                memory_consolidation_log.c.status == "running",
            ))
        ).first()

        if running:
            elapsed = (now_cn() - running.started_at).total_seconds()
            if elapsed < settings.memory.consolidation.lock_timeout_seconds:
                return None  # 锁被持有，跳过
            # 超时 → 标记为 failed，回收锁
            conn.execute(
                update(memory_consolidation_log)
                .where(memory_consolidation_log.c.id == running.id)
                .values(status="failed", error_message="lock timeout")
            )

        # 创建新的 running 记录 = 获取锁
        log_id = str(uuid.uuid4())
        conn.execute(
            memory_consolidation_log.insert().values(
                id=log_id, user_id=user_id,
                started_at=now_cn(), status="running",
            )
        )
        return log_id
```

#### E10c. 调度入口

```python
# core/orchestrator/orchestrator.py — _post_analyze() 末尾

async def _maybe_trigger_consolidation(self, user_id: str) -> None:
    """检查是否需要触发记忆整合，满足条件时提交异步任务。"""
    if not self._settings.memory.consolidation.enabled:
        return

    from core.memory.consolidation import should_consolidate, run_consolidation

    if not await should_consolidate(user_id, self._settings):
        return

    # 提交异步任务（fire-and-forget，不阻塞分析响应）
    import asyncio
    asyncio.create_task(
        run_consolidation(user_id, self._settings),
        name=f"consolidation_{user_id}",
    )
    _logger.info("consolidation task submitted for user=%s", user_id)
```

---

### E11. 整合执行逻辑

#### E11a. entity_profile 合并规则

**目标**：同一实体的多条画像记忆合并为一条，保留最新指标，追加历史趋势。

```
整合前：
  mem_001: "SUP-003 三路匹配率 92%, 2026-04-05"
  mem_002: "SUP-003 三路匹配率 88.5%, 2026-04-12"
  mem_003: "SUP-003 三路匹配率 85%, 2026-04-19"

整合后（1 条）：
  mem_new: "SUP-003 最新三路匹配率 85%（2026-04-19）。
           历史趋势：92%→88.5%→85%，持续下降。涉及 15 笔 PO，
           主要供应电子元器件。风险标签：价格异常、匹配率下降"
  attrs.source_ids = ["mem_001", "mem_002", "mem_003"]
  is_consolidated = true
```

**纯规则模式**（LLM 关闭时）：

```python
def merge_entity_profiles_rule(profiles: list[dict]) -> dict:
    """按实体 ID 分组，取最新指标，拼接历史。"""
    # 按 entity_id 分组
    by_entity = defaultdict(list)
    for p in profiles:
        eid = p["attrs"].get("entity_id")
        if eid:
            by_entity[eid].append(p)

    merged = []
    for entity_id, group in by_entity.items():
        if len(group) < 2:
            continue  # 单条无需合并

        # 按时间排序
        group.sort(key=lambda x: x["created_at"])
        latest = group[-1]

        # 拼接内容：最新指标 + 历史趋势摘要
        history_lines = [m["content"] for m in group[:-1]]
        content = (
            f"{latest['content']}\n"
            f"历史记录（{len(group)-1}条）：{'；'.join(history_lines[:3])}"
        )

        merged.append({
            "content": content,
            "attrs": {**latest["attrs"], "observation_count": len(group)},
            "source_ids": [m["id"] for m in group],
            "entity_id": entity_id,
        })
    return merged
```

**LLM 模式**（开启时）：将同一实体的多条记忆交给 llm_fast，生成语义合并的画像。

#### E11b. analysis_insight 归纳规则

**目标**：3 条以上同类发现合并为 1 条趋势摘要。

**分组依据**：`attrs.related_analysis_types` + `attrs.related_entities` 交集。

```
整合前：
  ins_001: "4月5日 SUP-003 三路匹配异常 2 条"
  ins_002: "4月12日 SUP-003 三路匹配异常 4 条"
  ins_003: "4月19日 SUP-003 三路匹配异常 6 条"

整合后（1 条）：
  ins_new: "SUP-003 三路匹配异常呈上升趋势：2条→4条→6条（4月5日~4月19日），
           建议重点关注"
  attrs.observation_count = 3
  attrs.trend_direction = "worsening"
```

**纯规则模式**：按 `related_analysis_types` 分组，计数 ≥3 的组做简单拼接。

**LLM 模式**：将同组发现交给 llm_fast，生成趋势归纳。Prompt 要求输出趋势方向（improving / stable / worsening）和摘要。

#### E11c. correction 与 domain_fact 冲突检测

**目标**：检测 correction 是否推翻了已有的 domain_fact，或多条 correction 是否可提炼为新的 domain_fact。

**规则 1：correction 推翻 domain_fact**

```
domain_fact: "三路匹配容差阈值为 5%"
correction:  "用户说容差应该是 2%，5% 太宽"

→ 将 domain_fact 标记为 superseded（软删除），correction 提炼为新 domain_fact
```

**规则 2：多条 correction 提炼为 domain_fact**

```
correction_001: "PO-10045 价格差异是批量折扣"
correction_002: "PO-10078 价格差异也是批量折扣"
correction_003: "PO-10102 同样是批量折扣导致"

→ 提炼为 domain_fact: "SUP-003 的大批量订单享有折扣，价格差异分析应排除"
```

**实现方式**：

- 纯规则模式：按 `correction_type` 和 `related_entities` 分组，计数 ≥3 且 `correction_type` 相同 → 标记为候选提炼，生成模板化 domain_fact
- LLM 模式：将 corrections 和相关 domain_facts 交给 llm_fast，判断冲突并生成更新后的 domain_fact

#### E11d. LLM 合并 vs 纯规则模式的开关与流程差异

**配置项**：

```yaml
memory:
  consolidation:
    llm_enabled: true   # LLM 语义合并开关
```

**流程对比**：

```
run_consolidation(user_id)
  │
  ├─ 1. 获取锁（E10b）
  ├─ 2. 加载该用户所有未整合记忆
  ├─ 3. 按类型分桶
  │
  ├─ if llm_enabled:
  │    ├─ 4a. entity_profile → LLM 合并（一次调用合并同实体的所有记忆）
  │    ├─ 5a. analysis_insight → LLM 归纳（一次调用归纳同组发现）
  │    ├─ 6a. correction + domain_fact → LLM 冲突检测 + 提炼
  │    └─ 7a. user_preference → LLM 合并同维度偏好
  │
  ├─ else (纯规则模式):
  │    ├─ 4b. entity_profile → 规则合并（取最新指标 + 拼接历史）
  │    ├─ 5b. analysis_insight → 规则归纳（计数分组 + 模板拼接）
  │    ├─ 6b. correction + domain_fact → 规则冲突检测（关键词匹配 + 计数阈值）
  │    └─ 7b. user_preference → 规则合并（同 key 取最新值）
  │
  ├─ 8. 写入整合产物（is_consolidated=true, source_ids=[...])
  ├─ 9. 软删除被合并的源记忆（或标记 consolidated_at）
  ├─ 10. TTL 淘汰（E12a）
  ├─ 11. 更新 consolidation_log（status=completed）
  └─ 12. 释放锁
```

**LLM 调用预算**：整合过程中每种类型最多 1 次 LLM 调用（批量处理），单次整合总计最多 4 次 llm_fast 调用。超时由 `consolidation_llm_timeout_seconds`（默认 60s）控制，单次超时则该类型回退到规则模式。

---

### E12. TTL 与淘汰策略

#### E12a. TTL 配置与过期扫描机制

**各类型 TTL 默认值**：

| 类型 | TTL | 理由 |
|------|-----|------|
| user_preference | 不过期（0） | 用户偏好相对稳定 |
| entity_profile | 90 天 | 业务数据有时效性，3 个月前的指标可能已不准确 |
| analysis_insight | 60 天 | 趋势有时效性，过旧的趋势可能已反转 |
| correction | 180 天 | 修正信息保留较长，防止同类错误重复 |
| domain_fact | 不过期（0） | 业务规则相对稳定，需人工主动删除 |

**过期扫描时机**：在整合任务的最后一步执行（E11d 步骤 10），不单独调度。

```python
async def _purge_expired(self, user_id: str) -> int:
    """删除已过期的记忆，返回删除条数。"""
    stmt = (
        memories_table.delete()
        .where(and_(
            memories_table.c.user_id == user_id,
            memories_table.c.expires_at.isnot(None),
            memories_table.c.expires_at < now_cn(),
        ))
        .returning(memories_table.c.id)
    )
    with self._engine.begin() as conn:
        deleted_ids = [row[0] for row in conn.execute(stmt)]

    # 清理向量索引（best-effort）
    if deleted_ids and self._vector_store.get():
        try:
            self._vector_store.get().delete(
                ids=[f"memory_{mid}" for mid in deleted_ids]
            )
        except Exception as exc:
            _logger.warning("vector cleanup failed: %s", exc)

    return len(deleted_ids)
```

**TTL 写入时机**：在 `MemoryRepository.save()` 中，根据 `memory_type` 自动计算 `expires_at`。

```python
def _compute_expires_at(self, memory_type: str) -> datetime | None:
    ttl_days = self._ttl_config.get(memory_type, 0)
    if ttl_days <= 0:
        return None
    return now_cn() + timedelta(days=ttl_days)
```

#### E12b. cap 淘汰与整合产物保护

**现有 cap 机制**：`_enforce_user_cap` 按 `created_at DESC` 保留前 N 条（`max_per_user=200`），超出的删除。

**增强**：整合产物（`is_consolidated=true`）应受保护，不被 cap 淘汰优先删除。

```python
def _enforce_user_cap(self, user_id: str) -> None:
    """滚动 cap：保留最新 N 条，优先淘汰非整合记忆。"""
    cap = self._max_per_user
    with self._engine.begin() as conn:
        # 优先淘汰非整合记忆
        stale_stmt = (
            select(memories_table.c.id)
            .where(and_(
                memories_table.c.user_id == user_id,
                memories_table.c.is_consolidated == False,  # noqa: E712
            ))
            .order_by(memories_table.c.created_at.desc())
            .offset(cap)
        )
        stale_ids = [row[0] for row in conn.execute(stale_stmt)]

        if not stale_ids:
            # 非整合记忆不足 cap，检查总量（含整合记忆）
            total_stmt = (
                select(memories_table.c.id)
                .where(memories_table.c.user_id == user_id)
                .order_by(memories_table.c.created_at.desc())
                .offset(cap)
            )
            stale_ids = [row[0] for row in conn.execute(total_stmt)]

        if stale_ids:
            conn.execute(
                memories_table.delete().where(memories_table.c.id.in_(stale_ids))
            )
```

**设计要点**：整合记忆是多条源记忆的精华，信息密度高。cap 淘汰时先删非整合记忆，整合记忆最后才被淘汰。

---

## F. 可观测性与韧性

### F13. 记忆专属监控指标

#### F13.0 双 trace 架构：analyze trace 与 memory trace 分离

记忆的详细监控与分析请求的全链路监控保持相对独立。analyze trace 中保留粗粒度的 `memory_ref` span 作为关联锚点，详细的记忆 span 记录在独立的 memory trace 中。

**为什么分离：**

1. **trace 膨胀**：记忆细节（提取 5 种类型、双通道检索、整合每步合并）会让 analyze trace 从 5~8 个 span 膨胀到 20+，排查分析性能时被淹没
2. **生命周期不匹配**：整合和摘要是 fire-and-forget，analyze trace 已结束它们还在运行，span 无法正确闭合
3. **关注点不同**："为什么分析慢"看 analyze trace；"为什么记忆不准"看 memory trace

**双 trace 结构：**

```
analyze trace（trace_id = T1）                 memory trace（trace_id = M1）
  ├─ span(intent_routing)                        ├─ span(memory.search_by_type)
  ├─ span(entity_resolve)                        │    ├─ span(search.entity_profile.exact)
  ├─ span(dag_execute)                           │    ├─ span(search.correction.exact)
  ├─ span(report_generate)                       │    ├─ span(search.correction.semantic)
  │                                              │    └─ span(search.domain_fact.exact)
  └─ span(memory_ref)  ← 粗粒度锚点              ├─ span(memory.inject)
       attrs: {                                  ├─ span(memory.extract)
         memory_trace_id: "M1",                  │    ├─ span(extract.entity_profile)
         memory_context_tokens: 1250,            │    └─ span(extract.analysis_insight)
         types_injected: ["entity_profile",      ├─ span(memory.feedback.detect)
                          "correction"],          ├─ span(memory.feedback.extract)
         search_duration_ms: 45,                 ├─ span(memory.summary)
         extract_fired: true,                    └─ span(memory.consolidation)
         consolidation_fired: false                   ├─ span(consolidation.merge.entity_profile)
       }                                              ├─ span(consolidation.conflict_detect)
                                                      └─ span(consolidation.purge)
```

**关联方式**：analyze trace 的 `memory_ref` span 通过 `memory_trace_id` 属性指向 memory trace。查询时可从 T1 跳转到 M1 查看详情。

**实现方式**：

```python
# core/memory/manager.py — MemoryManager 入口生成独立 trace_id

class MemoryManager:
    def _create_memory_trace_id(self) -> str:
        """为本次记忆操作生成独立的 trace_id。"""
        return f"mem_{uuid.uuid4().hex[:12]}"

    async def build_context(self, user_id, query, parsed_params, analysis_type):
        mem_trace_id = self._create_memory_trace_id()
        # 所有子操作共用此 mem_trace_id
        with record_span("memory_ref", "analyze_memory_ref",
                         memory_trace_id=mem_trace_id) as ref_span:
            result = await self._search_and_format(
                user_id, query, parsed_params, analysis_type,
                trace_id=mem_trace_id,  # 透传到子组件
            )
            ref_span["memory_context_tokens"] = estimate_tokens(result)
            return result
```

**存储**：analyze trace 和 memory trace 写入同一张 `traces` 表，按 `trace_id` 过滤区分。现有 `/traces` API 无需改造，前端按 trace_id 查询即可。

**查询场景对照：**

| 场景 | 查哪个 trace | 看什么 |
|------|-------------|--------|
| "这次分析为什么慢" | analyze trace (T1) | `memory_ref.search_duration_ms` 是否超时 |
| "为什么这条 correction 没被召回" | memory trace (M1) | `search.correction.exact` 是否命中、`search.correction.semantic` 排序 |
| "整合为什么把两条记忆合并了" | memory trace (M1) | `consolidation.merge.entity_profile.source_ids` |
| "LLM 摘要耗时多少" | memory trace (M1) | `memory.summary.duration_ms` + `llm_tokens` |

---

#### F13a. 写入/检索/注入相关 span（记录在 memory trace 中）

| span 名称 | 触发点 | 关键属性 |
|-----------|--------|---------|
| `memory.extract` | MemoryExtractor.extract() | user_id, memory_types_written, entity_ids, duration_ms |
| `memory.extract.entity_profile` | _extract_entity_profile() | entity_id, metrics_snapshot, source_analysis_type |
| `memory.extract.analysis_insight` | _extract_analysis_insight() | pattern_type, trend_direction, observation_count |
| `memory.feedback.detect` | FeedbackDetector.detect_signal() | signal_type (correction/preference/fact/none), query_len |
| `memory.feedback.extract` | FeedbackDetector.extract() | feedback_type, confidence, llm_tokens, duration_ms |
| `memory.search_by_type` | MemoryRepository.search_by_type() | user_id, query_len, type_counts (各类型命中数), duration_ms |
| `memory.inject` | format_memory_injection() | total_tokens, type_token_counts, types_injected |

#### F13b. 整合/摘要/淘汰相关 span

| span 名称 | 触发点 | 关键属性 |
|-----------|--------|---------|
| `memory.consolidation` | run_consolidation() | user_id, input_count, merged_count, pruned_count, llm_used, duration_ms, status |
| `memory.consolidation.merge` | 各类型合并步骤 | memory_type, source_count, output_count, llm_used |
| `memory.consolidation.conflict` | correction-domain_fact 冲突检测 | conflicts_found, facts_superseded, facts_created |
| `memory.consolidation.purge` | _purge_expired() | expired_count, vector_cleanup_ok |
| `memory.summary` | ShortTermMemory.summarize_and_replace() | session_id, input_chars, output_tokens, llm_model, duration_ms, status (ok/error/skipped) |
| `memory.summary.fallback` | 摘要失败回退截断 | error_type, fallback_chars |

**计数器**（复用现有 `_inc` 机制）：

| 计数器 key | 含义 |
|-----------|------|
| `ltm.extract.written` | 记忆提取写入成功次数 |
| `ltm.extract.skipped` | 记忆提取跳过次数（无实体/无趋势） |
| `ltm.feedback.detected` | 反馈信号检测命中次数 |
| `ltm.feedback.extracted` | 反馈 LLM 提取成功次数 |
| `ltm.consolidation.completed` | 整合完成次数 |
| `ltm.consolidation.failed` | 整合失败次数 |
| `ltm.consolidation.lock_timeout` | 整合锁超时回收次数 |
| `ltm.summary.completed` | 摘要完成次数 |
| `ltm.summary.fallback` | 摘要失败回退截断次数 |
| `ltm.ttl.purged` | TTL 淘汰记忆条数 |

---

### F14. 降级与兜底策略

| 环节 | 失败场景 | 降级行为 | 日志级别 |
|------|---------|---------|---------|
| **记忆提取** | MemoryExtractor 异常 | 跳过提取，分析结果正常返回用户 | WARNING |
| **反馈检测** | 关键词预筛异常 | 跳过反馈检测，不影响路由 | WARNING |
| **反馈 LLM 提取** | llm_fast 超时/异常 | 跳过提取，用户反馈不写入长期记忆 | WARNING |
| **LLM 摘要** | llm_fast 超时/异常 | 保留截断版本在 checkpointer 中 | WARNING |
| **长期记忆检索** | MemoryRepository.search_by_type 异常 | 返回空记忆上下文，分析不注入历史记忆 | WARNING |
| **长期记忆注入** | format_memory_injection 异常 | 返回空字符串，prompt 中无记忆部分 | WARNING |
| **向量写入** | Chroma 不可用 | 纯 SQL 模式，hybrid 检索退化为 FTS only | WARNING（已有 _VectorStoreProxy 退避） |
| **整合触发** | should_consolidate SQL 异常 | 跳过本次触发判定，下次分析再检查 | WARNING |
| **整合锁获取** | 锁被持有 | 跳过本次整合 | INFO |
| **整合执行** | LLM 调用超时 | 该类型回退到纯规则模式继续整合 | WARNING |
| **整合执行** | 规则合并异常 | 跳过该类型，其他类型继续 | WARNING |
| **整合执行** | 全部失败 | 更新 consolidation_log status=failed，释放锁 | ERROR |
| **TTL 淘汰** | DELETE 异常 | 跳过本次淘汰，下次整合再处理 | WARNING |
| **向量清理** | Chroma delete 异常 | DB 记录已删除，向量残留（下次写入时会被覆盖） | WARNING |

**核心原则**：记忆系统的任何环节失败都**不阻塞分析主流程**。所有记忆相关操作对用户透明——分析结果的正确性不依赖记忆系统的可用性。记忆系统是"锦上添花"，不是"必要条件"。

---

## G. 配置体系

### G15. 完整 config.yaml 变更项

```yaml
memory:
  short_term:
    max_messages: 20                    # [保留] 滑动窗口最大消息数
    summary_threshold: 15               # [保留] 超过N条消息触发摘要压缩
    context_trim_enabled: true          # [保留] 注入 LLM 前的 token 裁剪兜底开关
    context_max_tokens_pct: 15          # [保留] 短期记忆最大占 context_window 的百分比
    # --- 新增 ---
    summary_enabled: true               # [新增] LLM 异步摘要总开关
    summary_max_input_chars: 5000       # [新增] 超过此长度才触发摘要
    summary_max_output_tokens: 500      # [新增] 摘要输出 token 上限
    summary_timeout_seconds: 30         # [新增] LLM 摘要调用超时

  long_term:
    enabled: true                       # [保留] 长期记忆主路径总开关
    max_retrieved: 5                    # [保留] Hybrid 检索最多返回N条（兼容旧接口）
    context_trim_enabled: true          # [保留] 注入 LLM 前的 token 裁剪兜底开关
    context_max_tokens_pct: 12          # [变更] 10→12，适配5种类型
    fusion_k: 60                        # [保留] RRF 融合的平滑常数
    max_per_user: 200                   # [保留] 每个用户保留的最大记忆条数
    min_content_len: 50                 # [保留] 短于该长度的内容直接 skip
    dedupe_window_seconds: 900          # [保留] 同 user + 同 content_hash 的去重时间窗口
    skip_empty_conclusions: false       # [保留] 跳过空结论
    search_timeout_seconds: 0.2         # [新增] 检索注入整体超时（秒），超时返回空记忆
    # --- 新增 ---
    type_budget_pct:                    # [新增] 各类型 token 子预算（占 long_term 总预算百分比）
      entity_profile: 40
      user_preference: 20
      analysis_insight: 15
      correction: 15
      domain_fact: 10
    type_max_retrieved:                 # [新增] 各类型检索最大条数
      entity_profile: 3
      user_preference: 2
      analysis_insight: 2
      correction: 2
      domain_fact: 3

  react:
    trim_enabled: true                  # [保留] ReAct 循环内 LLM 输入裁剪总开关
    keep_recent_rounds: 2               # [保留] 保留最近几轮完整对话
    tool_content_max_chars: 500         # [保留] 早期 ToolMessage 截断字符数

  # --- 新增配置块 ---
  ttl:                                  # [新增] 各类型 TTL（天数，0=不过期）
    entity_profile_days: 90
    analysis_insight_days: 60
    correction_days: 180
    user_preference_days: 0
    domain_fact_days: 0

  consolidation:                        # [新增] 记忆整合配置
    enabled: true                       # 整合总开关
    min_new_memories: 20                # 触发条件：新增记忆数阈值
    max_interval_hours: 72              # 触发条件：最大时间间隔（小时）
    lock_timeout_seconds: 600           # 整合锁超时（秒）
    llm_enabled: true                   # LLM 语义合并开关
    llm_timeout_seconds: 60             # 单次 LLM 调用超时

  feedback:                             # [新增] 用户反馈检测配置
    enabled: true                       # 反馈检测总开关
    min_confidence: 0.6                 # LLM 提取最低置信度
```

---

## H. 实施评估

### H16. 改动点清单

#### 新增文件（6 个）

| 文件 | 职责 | 代码量估算 |
|------|------|-----------|
| `core/memory/types.py` | MemoryType 枚举 + 类型配置 | ~30 行 |
| `core/memory/extractor.py` | MemoryExtractor（分析结果 → 结构化记忆） | ~250 行 |
| `core/memory/feedback.py` | FeedbackDetector（用户反馈检测 + LLM 提取） | ~200 行 |
| `core/memory/injection.py` | 记忆注入格式化（search → prompt 文本） | ~100 行 |
| `core/memory/consolidation.py` | 整合引擎（触发判定 + 锁 + 合并 + 淘汰） | ~400 行 |
| `migrations/versions/0010_memory_type_system.py` | Alembic 迁移脚本 | ~80 行 |

#### 改造文件（7 个）

| 文件 | 改动内容 | 改动量估算 |
|------|---------|-----------|
| `core/memory/tables.py` | memories 表新增列 + memory_consolidation_log 表定义 | ~40 行 |
| `core/memory/long_term.py` | MemoryRepository 新增 search_by_type、save 增加 entity_id/expires_at 参数、_enforce_user_cap 增强 | ~120 行 |
| `core/memory/short_term.py` | ShortTermMemory 新增 summarize_and_replace 方法 | ~80 行 |
| `core/orchestrator/orchestrator.py` | _post_analyze 追加 extract_memories + _maybe_trigger_consolidation；analyze 流程追加 _build_memory_context | ~60 行 |
| `modules/p2p/agent.py` | _truncate_checkpointer_history 增强（截断前实体保全） | ~20 行 |
| `config/settings.py` | 新增 TTLSettings / ConsolidationSettings / FeedbackSettings Pydantic 模型 | ~60 行 |
| `config/config.yaml` | 新增配置项（G15 所列） | ~30 行 |

#### DB 迁移（1 个）

- `0010_memory_type_system.py`：memories 表 5 个新列 + 4 个新索引 + memory_consolidation_log 新表

#### 测试文件（预计新增 4 个）

| 文件 | 覆盖范围 |
|------|---------|
| `tests/unit/test_extractor.py` | MemoryExtractor 各类型提取逻辑 |
| `tests/unit/test_feedback.py` | FeedbackDetector 预筛 + LLM 提取 |
| `tests/unit/test_consolidation.py` | 整合引擎（触发 + 锁 + 合并 + 淘汰） |
| `tests/unit/test_injection.py` | 记忆注入格式化 + token 预算分配 |

---

### H17. 改动成本评估

| 维度 | 评估 |
|------|------|
| **总代码量** | 新增 ~1060 行 + 改造 ~410 行 ≈ **1470 行**（不含测试） |
| **测试代码量** | 预计 ~800 行（4 个测试文件） |
| **工作量** | 约 **5~7 个工作日**（含测试 + 迁移 + 联调） |
| **风险等级** | **中等** |

**风险点：**

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| memories 表结构变更影响现有查询 | 新增列默认 NULL，不影响现有读写 | 迁移脚本幂等，downgrade 可回滚 |
| 整合任务长时间运行 | 占用 DB 连接和 LLM 资源 | 锁超时 600s + LLM 超时 60s + 纯规则回退 |
| LLM 摘要替换 checkpointer 的竞态 | 用户追问时读到截断版 | 截断版可用，最终一致 |
| 反馈检测误判 | 错误写入 correction/preference | confidence 阈值 0.6 + 预筛关键词可调 |
| 配置项增多 | 运维复杂度上升 | 所有新配置有生产默认值，零配置可用 |

**依赖：**
- 无新增外部依赖
- 复用现有 llm_fast 模型配置
- 复用现有 TaskRegistry / record_span / _VectorStoreProxy

---

### H18. 分阶段实施计划建议

#### Phase 1：基础设施 + MemoryManager 骨架（2 天）

**目标**：搭建类型体系、DB 变更、配置扩展、MemoryManager 空壳，确保现有测试全部通过。

- 新增 `core/memory/types.py`（MemoryType 枚举）
- 改造 `core/memory/tables.py`（memories 新增列 + memory_consolidation_log 新表）
- 新增 Alembic 迁移脚本 `0010_memory_type_system.py`
- 改造 `config/settings.py`（TTLSettings / ConsolidationSettings / FeedbackSettings）
- 改造 `config/config.yaml`（新增配置块）
- 改造 `core/memory/long_term.py`（save 方法增加 entity_id / expires_at 参数，向后兼容）
- 新增 `core/memory/manager.py`（MemoryManager 骨架，5 个方法暂为透传/空实现）
- **验收**：`pytest` 全部通过，MemoryManager 可实例化但不改变现有行为

#### Phase 2：MemoryManager 接入 + 写入管线（2 天）

**目标**：Orchestrator 切换到 MemoryManager，分析完成后可提取 entity_profile / analysis_insight。

- 改造 `core/orchestrator/orchestrator.py`：
  - `__init__` 中 `self._memory = MemoryManager(settings)` 替代 `self._short_term`
  - `analyze()` 三条路径（DAG / ReAct / Lookup）统一在出口调用 `self._memory.on_analysis_complete()`
  - 删除散落的记忆模块直接 import
- 新增 `core/memory/extractor.py`（MemoryExtractor）
- 新增 `core/memory/feedback.py`（FeedbackDetector）
- MemoryManager 实现 `on_analysis_complete()` 和 `on_user_message()`
- 测试：test_memory_manager.py + test_extractor.py + test_feedback.py
- **验收**：分析完成后 memories 表出现 entity_profile / analysis_insight 类型记录

#### Phase 3：检索与注入（1 天）

**目标**：分析前注入长期记忆上下文，精确优先 + 语义补充 + 超时保护。

- 改造 `core/memory/long_term.py`（search_by_type + _dual_channel_search）
- 新增 `core/memory/injection.py`（format_memory_injection）
- MemoryManager 实现 `build_context()`（带超时保护）
- 测试：test_injection.py
- **验收**：分析 prompt 中出现 `[历史修正记录]` / `[业务规则]` / `[相关实体历史画像]` 等结构化区块

#### Phase 4：短期记忆增强（1 天）

**目标**：分析结果异步摘要 + 截断前实体保全。

- 改造 `core/memory/short_term.py`（summarize_and_replace）
- 改造 `modules/p2p/agent.py`（_truncate_checkpointer_history 实体保全）
- MemoryManager 的 `on_analysis_complete()` 补充摘要触发
- 测试：扩展现有 short_term 测试
- **验收**：checkpointer 中的长分析结果被摘要替换；截断前实体写入 session_entities

#### Phase 5：整合引擎（1~2 天）

**目标**：后台整合（合并/归纳/冲突检测/TTL 淘汰）完整运行。

- 新增 `core/memory/consolidation.py`（ConsolidationEngine）
- 改造 `core/memory/long_term.py`（_enforce_user_cap 整合产物保护）
- MemoryManager 的 `on_analysis_complete()` 补充整合触发
- 测试：test_consolidation.py
- **验收**：手动触发整合后，同实体 entity_profile 合并为 1 条，过期记忆被淘汰

#### Phase 6：可观测性 + 端到端验证（1 天）

**目标**：双 trace 架构落地，端到端全链路验证。

- MemoryManager 生成独立 memory_trace_id，analyze trace 中记录粗粒度 `memory_ref` span
- 各子模块补充细粒度 span（F13a / F13b 所列）
- 端到端验证：分析 → 提取 → 检索注入 → 追问（摘要生效）→ 反馈 → 整合 → 淘汰
- **验收**：`/traces` API 可查询独立的 memory trace，从 analyze trace 可跳转

**总工期**：8~9 个工作日（含测试 + 迁移 + 联调）

---

## I. 路由架构重构：统一 LLM 调用

### I1. 重构动机

现有三级路由（L1 关键词 → L2 向量 → L3 LLM）+ 独立 ParamExtractor 存在根本缺陷：

1. **跨实体查询无法处理**："查询这个支付单对应的 PO"被 L1 关键词"po"匹配到 query_purchase_orders，但不理解"对应的"是跨实体关联
2. **两次 LLM 调用**：L3 分类 (~800ms) + ParamExtractor (~800ms) = ~1600ms，但 ParamExtractor 已不可省略
3. **L1/L2 命中率有限**：L1 分析类型命中约 60%~70%，未命中仍要走 L3；L2 向量匹配命中率更低
4. **补丁式修复不可持续**：每发现一种新的跨实体/复杂查询模式就要加正则/规则

### I2. 重构方案

**保留 L0 bypass + 统一 LLM 调用，删除 L1 分析类型匹配和 L2 向量查询。**

```
用户 query
  │
  ├─ L0 bypass（保留，零成本，~1ms）
  │    ├─ CHITCHAT（严格正则匹配）→ 模板响应
  │    ├─ META（子串匹配）→ 模板响应
  │    └─ RECALL（回溯词匹配，无强分析意图）→ 回溯历史
  │
  └─ 统一 LLM 调用（一次完成分类 + 参数提取，~1000ms）
       │
       输入：query + 当前日期 + session_entities（指代消解用）
       │
       输出 JSON：
       {
         "intent_kind": "analysis|data_lookup|clarification|out_of_scope",
         "analysis_type": "three_way_match|price_variance|...|",
         "confidence": 0.0~1.0,
         "is_cross_entity": false,
         "entities": {
           "po_number": null,
           "supplier_id": "SUP-003",
           "payment_number": "PAY-2024-0054",
           "invoice_number": null,
           "days": 30,
           "limit": 1,
           "order_by": "date_desc"
         },
         "resolved_query": "查询支付单 PAY-2024-0054 对应的采购订单",
         "missing_params": []
       }
       │
       ├─ DATA_LOOKUP + !is_cross_entity → Lookup 快捷路径
       ├─ DATA_LOOKUP + is_cross_entity  → ReAct（多步推理）
       ├─ ANALYSIS → DAG 或 ReAct（按 confidence 决策）
       ├─ CLARIFICATION → 追问模板
       └─ OUT_OF_SCOPE → 拒绝模板
```

### I3. 统一 LLM Prompt 设计

```
你是 ERP 采购分析系统的意图解析器。一次性完成以下任务：
1. 判定用户意图类型（intent_kind）
2. 识别分析类型（analysis_type）
3. 提取所有实体和参数
4. 判断是否为跨实体关联查询
5. 完成指代消解（结合 session_entities 上下文）

## 输入
- 用户查询：{query}
- 当前日期：{date}
- 会话实体上下文：{session_entities}（上一轮查询涉及的实体）

## 输出（严格 JSON）
{schema}

## 关键规则
1. is_cross_entity=true 当查询涉及从一种实体跳转到另一种（如"这个付款单的PO"/"这个供应商的发票"）
2. 指代消解：结合 session_entities，将"这个/该/上次"替换为具体实体 ID，写入 resolved_query
3. entities 中只填能确定的具体值，不确定的填 null
4. limit/order_by：从"最新的一个""前 N 条"等表述中提取
5. days：从"最近 N 天""本月"等时间表述中计算
```

### I4. 变更影响

| 组件 | 变更 | 说明 |
|------|------|------|
| `core/orchestrator/router/__init__.py` | **大幅简化** | 删除 L1 分析类型匹配 + L2 向量查询，保留 L0 bypass + 统一 LLM 调用 |
| `core/orchestrator/param_extractor.py` | **删除** | 合并进统一 LLM 调用 |
| `config/intent_seeds.yaml` | **删除** | L2 向量查询不再需要 |
| `core/orchestrator/signal.py` | **扩展** | QuerySignal 新增 is_cross_entity / resolved_query / limit / order_by |
| `core/orchestrator/lookup.py` | **简化** | 删除 `_parse_query_constraints`（LLM 直接输出 limit/order_by），删除跨实体正则检测（LLM 直接判断 is_cross_entity） |
| `core/orchestrator/orchestrator.py` | **简化** | 删除 `_param_extractor` 调用，统一从 LLM 结果取参数 |
| `core/orchestrator/entity.py` | **简化** | 指代消解由统一 LLM 完成（session_entities 作为 prompt 输入），正则消解可保留作为 LLM 失败的兜底 |

### I5. 性能对比

| 维度 | 重构前 | 重构后 |
|------|--------|--------|
| LLM 调用次数 | 2（L3 + ParamExtractor） | **1** |
| 总延迟 | L1/L2(~5ms) + L3(~800ms) + Param(~800ms) = **~1600ms** | bypass(~1ms) + 统一 LLM(~1000ms) = **~1000ms** |
| Token 消耗 | L3 prompt ~200 + Param prompt ~300 = **~500 tok** | 统一 prompt **~400 tok**（合并后更紧凑） |
| 准确性 | L1 误匹配 + L3/Param 信息割裂 | **更高**（LLM 同时看到意图和实体） |
| 代码复杂度 | L1 + L2 + L3 + ParamExtractor = **4 个模块** | bypass + 统一 LLM = **1 个模块** |

### I6. 实施计划

#### Phase R1：统一 LLM 调用实现（2 天）
- 新增 `core/orchestrator/unified_router.py`（统一 LLM prompt + 解析）
- 扩展 `QuerySignal`（新增 is_cross_entity / resolved_query / limit / order_by）
- 改造 `IntentRouter.route()`：L0 bypass → 统一 LLM（删除 L1/L2/L3/ParamExtractor 调用链）

#### Phase R2：Orchestrator 适配（1 天）
- 删除 `self._param_extractor`
- `analyze()` 中使用统一 LLM 结果的 entities / is_cross_entity / resolved_query
- lookup 快捷路径用 `signal.is_cross_entity` 判断是否降级 ReAct

#### Phase R3：清理 + 测试（1 天）
- 删除 `core/orchestrator/param_extractor.py`
- 删除 `config/intent_seeds.yaml`
- 清理 `router/__init__.py` 中 L1 分析类型匹配 + L2 向量查询代码
- 更新所有相关测试
- 端到端验证：普通查询 / 跨实体查询 / 指代消解 / bypass

**总工期**：4 天

---

### H19. 改造后代码结构变化

#### 改造前 core/memory/ 目录

```
core/memory/
├── __init__.py              ← 导出 get_long_term_memory
├── long_term.py             ← LongTermMemory / MemoryRepository / ReportRepository
├── short_term.py            ← ShortTermMemory（checkpointer 管理）
├── trimmer.py               ← MemoryMiddleware（ReAct 循环内裁剪）
└── tables.py                ← memories / reports / session_entities 表定义
```

#### 改造后 core/memory/ 目录

```
core/memory/
├── __init__.py              ← 导出 get_long_term_memory（保持兼容）+ get_memory_manager
│
├── manager.py               ← [新增] MemoryManager — 统一入口
│                                Orchestrator 唯一依赖此类
│
├── types.py                 ← [新增] MemoryType 枚举（5 种封闭类型）
│
├── extractor.py             ← [新增] MemoryExtractor
│                                分析结果 → entity_profile / analysis_insight
│
├── feedback.py              ← [新增] FeedbackDetector
│                                用户反馈预筛 + LLM 提取 → correction / user_preference / domain_fact
│
├── injection.py             ← [新增] MemoryInjector
│                                search_by_type 双通道检索 + format_memory_injection 格式化
│
├── consolidation.py         ← [新增] ConsolidationEngine
│                                触发判定 + 锁 + 合并/归纳/冲突检测/淘汰
│
├── long_term.py             ← [改造] MemoryRepository 增强
│                                + save() 增加 entity_id / expires_at 参数
│                                + search_by_type() 双通道检索
│                                + _dual_channel_search() 精确优先+语义补充
│                                + _enforce_user_cap() 整合产物保护
│                                LongTermMemory facade / ReportRepository 不变
│
├── short_term.py            ← [改造] ShortTermMemory 增强
│                                + summarize_and_replace() 异步 LLM 摘要
│                                其他方法不变
│
├── trimmer.py               ← [不变] MemoryMiddleware
│
└── tables.py                ← [改造] memories 表新增列 + memory_consolidation_log 表
```

#### 模块依赖关系

```
Orchestrator
  │
  └─── MemoryManager (core/memory/manager.py)
         │
         ├── ShortTermMemory (short_term.py)
         │     └── LangGraph PostgresSaver
         │
         ├── MemoryInjector (injection.py)
         │     └── MemoryRepository.search_by_type (long_term.py)
         │           ├── _dual_channel_search → SQL 精确查询
         │           └── _search_by_type_hybrid → FTS + Chroma 向量
         │
         ├── MemoryExtractor (extractor.py)
         │     └── MemoryRepository.save (long_term.py)
         │
         ├── FeedbackDetector (feedback.py)
         │     ├── detect_signal → 关键词预筛（同步）
         │     └── extract → llm_fast 调用（异步）
         │           └── MemoryRepository.save (long_term.py)
         │
         └── ConsolidationEngine (consolidation.py)
               ├── should_consolidate → SQL 查询
               ├── try_acquire_lock → memory_consolidation_log
               ├── merge/归纳/冲突检测 → 规则 或 llm_fast
               └── purge_expired → SQL DELETE
```

#### 改造涉及的外部文件

```
config/
├── config.yaml              ← [改造] 新增 memory.ttl / consolidation / feedback 配置块
└── settings.py              ← [改造] 新增 TTLSettings / ConsolidationSettings / FeedbackSettings

core/orchestrator/
└── orchestrator.py          ← [改造] 删除 6 个记忆模块直接依赖
                                替换为 self._memory = MemoryManager(settings)
                                analyze() 中调用 5 个 MemoryManager 方法

modules/p2p/
└── agent.py                 ← [改造] _truncate_checkpointer_history 增强
                                截断前实体保全（约 20 行）

migrations/versions/
└── 0010_memory_type_system.py  ← [新增] Alembic 迁移脚本

tests/unit/
├── test_extractor.py        ← [新增]
├── test_feedback.py         ← [新增]
├── test_consolidation.py    ← [新增]
├── test_injection.py        ← [新增]
└── test_memory_manager.py   ← [新增]
```

#### 文件变更统计

| 类型 | 文件数 | 代码量估算 |
|------|--------|-----------|
| **新增**（core/memory/） | 6 个 | ~1200 行 |
| **新增**（migrations/） | 1 个 | ~80 行 |
| **新增**（tests/） | 5 个 | ~1000 行 |
| **改造** | 5 个 | ~350 行变更 |
| **总计** | 17 个文件 | ~2630 行（含测试） |

