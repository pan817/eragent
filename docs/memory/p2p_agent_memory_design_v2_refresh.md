# P2P Agent 记忆体系设计 — v2 Refresh

> 本文是对 [p2p_agent_memory_design.md](p2p_agent_memory_design.md)（v1）的**核查与刷新**，不是重写。原 v1 设计已基本实施，本次 refresh 聚焦：①已实施状态核查 ②原设计未覆盖的"跨会话长程对话记忆"补强 ③原设计与现状的偏差修订。
>
> **生成日期**：2026-05-01。
>
> **关系图**：
> ```
> v1 (p2p_agent_memory_design.md)
>   ├── 已实施 ✓ — 5 类 MemoryType / MemoryManager 入口 / 双通道检索 / 整合 / 双 trace / Unified LLM Router
>   ├── 实施偏差 ✗ — param_extractor 死代码遗留
>   └── 未覆盖 ✗ — 跨会话对话记忆（chat 索引、SESSION_RECAP、跨 session 实体消解、时间衰减）
>
> v2 Refresh (本文)
>   ├── §1 实施状态核查
>   ├── §2 与 long_term_chat_memory_design.md 的关系
>   ├── §3 新增章节 J — 跨会话长程对话记忆（按 v1 设计哲学重新设计）
>   ├── §4 对 v1 设计的修订（MemoryType 扩展 / 检索 / 集成）
>   ├── §5 死代码清理（param_extractor）
>   └── §6 实施评估与决策点
> ```
>
> **超越对象**：[../long_term_chat_memory_design.md](../long_term_chat_memory_design.md) 已并入本文 §3，原文档保留为历史记录但应视为废弃。

## 0. 刷新动机

之前在分析 [agent_enhancement_gaps.md §4.5](../agent_enhancement_gaps.md) 时，独立产出了 `long_term_chat_memory_design.md`，但**未先核查 v1 设计**，导致：

1. **绕过了 MemoryManager 单一入口** — v1 明确"Orchestrator 只依赖 MemoryManager 一个类"，v4.5 设计却新增了独立 ChatIndexer
2. **未套用"精确性优先于召回率"原则** — v1 第 D9 章设计了 `_dual_channel_search`（精确通道 + 语义补充），v4.5 用纯语义 + ILIKE
3. **未对齐双 trace 架构** — v1 F13.0 定义了 analyze trace 与 memory trace 分离，v4.5 没提
4. **未走 MemoryType 封闭枚举扩展** — v1 强调"5 种类型是封闭枚举，扩展需改代码 + 配置"，v4.5 直接加工具不加类型

本次刷新**重新设计 §3 J 章**对齐 v1 哲学，并补充 v1 设计中遗漏的"跨会话对话记忆"维度。

## 1. v1 设计实施状态核查

### 1.1 实施成功项（A - I 章）

| 章节 | 设计点 | 实施状态 | 验证依据 |
|------|--------|---------|---------|
| **A1** | 三层记忆架构（短期 / 长期 / 整合） | ✓ | 三层模块文件齐备 |
| **A1.1** | MemoryManager 单一入口（5 个方法） | ✓ | `core/memory/manager.py` 存在，`build_context` / `on_analysis_complete` / `on_user_message` / `load_session` / `save_session_entities` 五方法齐备 |
| **A1.1** | Orchestrator 只依赖 MemoryManager | ✓ | `core/orchestrator/orchestrator.py:152` 仅持有 `self._memory = MemoryManager(...)` |
| **A1.1** | 三条执行路径（DAG / ReAct / Lookup）统一出口 | ✓ | `orchestrator.py` 三处统一调用 `self._memory.on_analysis_complete` |
| **B3** | 5 种封闭 MemoryType 枚举 | ✓ | `core/memory/types.py` 完整定义 |
| **B4** | memories 表新增列（entity_id / expires_at / consolidated_at / is_consolidated / source_ids） | ✓ | migration 0011_memory_type_system 落地 |
| **B4.3** | memory_consolidation_log 表 | ✓ | `core/memory/tables.py` 中 `memory_consolidation_log_table` 已定义 |
| **C5** | 短期记忆 LLM 异步摘要 | ✓ | `short_term.py` 中 `summarize_and_replace` 方法 |
| **C6** | 截断前实体保全 | ✓ | `modules/p2p/agent.py::_truncate_checkpointer_history` 增强已落地 |
| **D7** | MemoryExtractor（结构化提取） | ✓ | `core/memory/extractor.py` 完整 |
| **D8** | FeedbackDetector（反馈预筛 + LLM 提取） | ✓ | `core/memory/feedback.py` 完整 |
| **D9** | search_by_type 双通道检索 | ✓ | `core/memory/long_term.py:760-922` 三个方法 |
| **D9** | format_memory_injection（按类型分区） | ✓ | `core/memory/injection.py` 完整 |
| **E10-E12** | 整合引擎（触发 / 锁 / 合并 / 淘汰） | ✓ | `core/memory/consolidation.py` 13KB |
| **F13** | 双 trace 架构（analyze trace / memory trace） | ✓ | `manager.py` `record_span("memory", ...)` + 独立 `mem_trace_id` |
| **F14** | 降级与兜底（13 处场景） | ✓ | `manager.py::build_context` 超时降级 + try/except 包裹 |
| **G15** | 配置体系扩展（TTL / consolidation / feedback） | ✓ | `config/settings.py` `MemorySettings` 含全部新字段 |
| **I1-I6** | 统一 LLM 路由（删除 L1/L2/L3 + ParamExtractor） | ✓（部分） | `unified_router.py` 已实施，`intent_seeds.yaml` 已删除；**但 `param_extractor.py` 文件 + 测试仍遗留**（见 §1.2） |

### 1.2 实施偏差（1 项）

**ParamExtractor 死代码遗留**：

- v1 设计 I4 章明确要求："`core/orchestrator/param_extractor.py` **删除** — 合并进统一 LLM 调用"
- 现状：`core/orchestrator/param_extractor.py`（包含 `ParamExtractor` 类）+ `tests/unit/test_param_extractor.py`（约 20 个测试用例）**仍存在**
- `core/orchestrator/orchestrator.py:160` 注释明确："ParamExtractor 已合并到统一 LLM 路由（UnifiedRouter），不再需要"
- 实际验证：grep 全代码库，仅 unified_router.py 文档字符串提及、orchestrator.py 注释提及、自身定义和测试 — 无任何代码路径调用
- **属于"已注释为完成但实际遗留的死代码"**

详细处置建议见 §5。

### 1.3 未覆盖项（v1 设计盲区）

v1 设计是**知识中心**（5 类聚焦"业务知识 / 偏好 / 修正 / 规则 / 实体画像"），未覆盖**对话中心**的能力：

| 缺口 | 业务影响 | 在 v1 中是否有占位 |
|------|---------|-------------------|
| chat_messages 未索引到向量库 | 跨会话语义检索不可用 | 无（仅 ChatRepository ILIKE） |
| 没有"会话摘要级"记忆类型 | 用户问"上个月在采购合规上得出过什么结论"无法回答 | 无（ANALYSIS_INSIGHT 是分析趋势，不是会话主题） |
| 跨 session 实体关联消解 | 用户说"那家供应商"无法回查上次会话 | session_entities 仅本会话 |
| Agent 工具集无 chat 历史检索工具 | RECALL 意图识别但无工具可调 | 无 |
| 仅硬过期 TTL，无时间衰减 | 跨周/跨月场景下旧记忆得分等同于新记忆 | E12a 仅 expires_at |

§3 J 章针对性补强这 5 项。

## 2. 与 long_term_chat_memory_design.md 的关系

之前的独立设计 `docs/long_term_chat_memory_design.md` 是 §4.5 enhancement gap 的初版方案，由于**未先读 v1**，存在以下与 v1 的对齐缺口：

| 维度 | long_term_chat_memory_design.md 原方案 | v1 设计要求 | 本次 refresh 修正 |
|------|----------------------------------------|------------|-----------------|
| **入口** | 新增独立 `core/chat/indexer.py` ChatIndexer | MemoryManager 单一入口（A1.1） | ChatIndexer 改为 MemoryManager 持有的内部组件，不暴露给 Orchestrator |
| **检索** | 纯语义 + ILIKE（无精确通道） | 「精确性优先于召回率」哲学第 2 条 + `_dual_channel_search` | chat history 检索复用 _dual_channel_search 模式：entity_id 精确通道 + 语义补充 |
| **类型** | 不新增 MemoryType，直接搞工具 | 类型封闭枚举，封闭性是行为契约（哲学第 4 条 + B3） | 新增 `SESSION_RECAP` 类型，遵循封闭枚举扩展规则 |
| **可观测性** | 仅工具 span | 双 trace 架构（F13.0） | chat 索引/检索 span 写入 memory trace |
| **TTL** | 自定义衰减 | 硬过期 + 整合保护（E12） | SESSION_RECAP 90 天硬过期 + 时间衰减权重（v1 未覆盖项，本次新增设计） |
| **路由** | RECALL → bypass → ReAct + 新工具 | I 章统一 LLM 已替换 bypass 路由架构 | RECALL 由 unified_router 输出 `intent_kind="recall"`，配合 chat history 工具 |

**结论**：`long_term_chat_memory_design.md` 应**视为废弃**，其内容经本文 §3 重新设计后融入。建议保留原文档作为历史记录（添加废弃标注），新工作以本文为准。

## 3. 新增章节 J — 跨会话长程对话记忆

### J1. 问题定义

**用户场景**：
- "上周我们讨论的那家供应商，本月还有异常发票吗？" — 跨 session 引用 + 实体消解 + 续推
- "继续上个月那个三路匹配差异分析" — 跨 session 续推
- "我之前说过的那个付款政策，再帮我查一遍" — 跨 session 知识回查

**当前不可用原因**：
- LangGraph PostgresSaver 短期记忆按 session_id 隔离 — 跨 session 失效
- 长期记忆 5 类是**知识维度**（preference / entity / insight / correction / fact），**不是会话维度**
- chat_messages 已持久化但未索引到向量库 — 关键词搜索（ILIKE）无法处理"那家化工厂" → SUP-007
- Agent 工具集没有"查我自己的历史会话"的工具 — 即使路由器识别 RECALL，Agent 也没工具可调

**问题边界**：
- 不重建 v1 已有的"用户偏好/实体画像/分析趋势/修正/事实" 5 类记忆
- 仅补强"对话主题级"+"对话片段级"的可检索能力
- 不引入用户主动管理记忆的 UI（保持隐式注入风格，与 v1 一致）

### J2. 设计原则（与 v1 哲学严格对齐）

#### J2.1 锦上添花（哲学第 1 条）

chat history 索引失败 / 检索超时 → 整体降级到 v1 现有的 5 类记忆 + 短期记忆 → 再降级到无记忆裸 ReAct。任何环节失败用户感知最多是"找不到上次的内容"，不会阻塞分析。

#### J2.2 精确性优先（哲学第 2 条）

ERP 场景下"上次说 SUP-003 的修正"误召为 SUP-004 比漏召更危险。chat history 检索应**复用 v1 D9 章的 `_dual_channel_search` 模式**：

```
chat history 检索（user query）
  │
  ├─ 通道 A（精确）：query 经 unified_router 抽取的 entity_ids 直接过滤
  │                  WHERE session_entities.entities @> '{"supplier_id": "SUP-003"}'
  │                  → 找到讨论过 SUP-003 的会话
  │
  └─ 通道 B（语义补充）：仅当通道 A 未填满时启用
                       Chroma 向量查询 chat_history collection（按 user_id 隔离）
                       → 召回语义相关但实体不明确的会话片段
```

不是"先全语义召回再 rerank"，而是"精确为主 + 语义补漏"。

#### J2.3 写入异步、检索同步、整合后台（哲学第 3 条）

| 操作 | 时机 | 模式 |
|------|------|------|
| chat 消息索引到向量库 | `ChatRepository.append_messages` 后 | 异步 fire-and-forget |
| chat history 检索（工具调用时） | Agent 工具调用同步执行 | 同步 + 200ms 超时 |
| 会话级摘要抽取 | session idle 30 分钟 | 异步任务（复用 TaskRegistry） |
| 时间衰减 rerank | 检索时同步计算 | 同步（纯数学，<1ms） |

#### J2.4 类型封闭（哲学第 4 条）

**新增 1 个 MemoryType：`SESSION_RECAP`**。这是封闭枚举的有意义扩展，需修改 `core/memory/types.py`、配置、迁移。

| 类型 | 维度 | 写入触发 | TTL | 与现有类型的边界 |
|------|------|---------|-----|----------------|
| **SESSION_RECAP**（新） | 单次会话的话题摘要 + 关键实体 + 主要结论 | session idle 30 min 或消息数 ≥ N | 90 天硬过期 + 时间衰减 | **不是** ANALYSIS_INSIGHT（业务发现/趋势），是"用户那次对话讨论了什么" |

边界示例对比：
- ANALYSIS_INSIGHT：「用户近 2 周连续 3 次查询付款合规，SUP-003 异常数量 2→4→6 上升趋势」
- SESSION_RECAP：「用户在 session_X 询问 SUP-003 的付款合规情况，结合三路匹配差异得出 6 笔异常付款应冻结的结论」

#### J2.5 生命周期（哲学第 5 条）

- chat_messages（SOT）：沿用 ChatRepository 现有软删除（用户主动删会话）
- chat_history Chroma 向量：向量丢失不影响正确性（退化为 ILIKE）
- SESSION_RECAP：与 ANALYSIS_INSIGHT 同级别（90 天）+ 时间衰减权重
- session_entities：保留现有"会话级 JSON"，新增"跨 session 索引"作为只读视图

#### J2.6 PostgreSQL 是 SOT（哲学第 6 条）

- chat_messages 已是 SOT 表（v1 中已落地）
- session_summaries 新表作为 SESSION_RECAP 的结构化字段镜像（便于 admin 查询）
- Chroma 是加速旁路；Chroma 故障 → 退化为 ILIKE 关键词

### J3. 体系定位（在 v1 三层架构中的位置）

```
┌──────────────────────────────────────────────────────────────┐
│ 第 1 层：短期记忆（会话内）          [v1 不变]                 │
│   LangGraph PostgresSaver + session_entities + ReAct 裁剪    │
└──────────────────────────────────────────────────────────────┘
                          ↑↓
┌──────────────────────────────────────────────────────────────┐
│ 第 2 层：长期记忆（跨会话）                                     │
│                                                              │
│  [v1 保留] memories 表 + Chroma                                │
│    ├─ user_preference / entity_profile / analysis_insight   │
│    ├─ correction / domain_fact                              │
│    └─ session_recap          ← [J 新增] 6 类                  │
│                                                              │
│  [v1 保留] reports 表                                          │
│                                                              │
│  [J 新增] chat_messages 向量索引（Chroma chat_history）        │
│    ├─ 写入：MemoryManager.on_user_message / on_assistant_msg  │
│    └─ 检索：MemoryManager.search_chat_history（工具入口）       │
│                                                              │
│  [J 新增] session_summaries 表                                 │
│    └─ SESSION_RECAP 的结构化镜像                               │
└──────────────────────────────────────────────────────────────┘
                          ↑↓
┌──────────────────────────────────────────────────────────────┐
│ 第 3 层：记忆整合（后台维护）                                    │
│                                                              │
│  [v1 保留] entity_profile / analysis_insight 合并             │
│  [v1 保留] correction ↔ domain_fact 冲突检测                   │
│                                                              │
│  [J 新增] SESSION_RECAP 时间衰减（不合并，仅 rerank 权重）       │
└──────────────────────────────────────────────────────────────┘
```

**关键设计点**：
- chat_messages 索引和 SESSION_RECAP 都在第 2 层（不新建第 4 层），保持 v1 三层结构
- MemoryManager 入口扩展两个方法：`search_chat_history` / `on_chat_message`
- ChatIndexer 是 MemoryManager 内部组件，不向 Orchestrator 暴露
- 工具 `search_my_chat_history` 通过 MemoryManager 访问数据，不直接读 Chroma

### J4. MemoryManager 接口扩展

v1 设计 MemoryManager 暴露 5 个方法（A1.1）。本次扩展 2 个方法 + 1 个内部组件。

```python
# core/memory/manager.py（在 v1 基础上扩展）

class MemoryManager:
    def __init__(self, settings: Settings) -> None:
        # v1 现有
        self._short_term = ShortTermMemory(settings)
        self._mem_repo: MemoryRepository | None = None
        self._extractor: MemoryExtractor | None = None
        self._feedback: FeedbackDetector | None = None

        # J 新增
        self._chat_indexer: ChatHistoryIndexer | None = None
        self._session_summarizer: SessionSummaryExtractor | None = None

    # ── v1 已有 5 方法保持不变 ────────────────────────────
    # build_context / on_analysis_complete / on_user_message
    # load_session / save_session_entities

    # ── J 新增方法 ────────────────────────────────────

    async def search_chat_history(
        self,
        user_id: str,
        query: str,
        entity_ids: list[str] | None = None,
        days: int = 30,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """跨会话历史对话检索。

        套用 v1 _dual_channel_search 模式：
        - 通道 A（精确）：entity_ids 命中 session_entities
        - 通道 B（语义补充）：Chroma chat_history collection 向量召回

        同步执行，带 200ms 超时保护。失败/超时返回空列表，不抛异常。
        被 modules/p2p/tools/chat_history.py 中的 @tool 调用。
        """

    def on_chat_message(
        self,
        session_id: str,
        message_id: str,
        role: str,
        content: str,
    ) -> None:
        """每条 chat 消息写入后触发的索引钩子。

        fire-and-forget：不阻塞 ChatRepository.append_messages 主流程。
        ChatHistoryIndexer 内部按"对话片段"聚合（user→assistant 一对），
        延迟 N 秒批量写入 Chroma。
        """

    # session_idle 触发（由 TaskRegistry 后台任务调用）
    async def on_session_idle(self, session_id: str) -> None:
        """会话不活跃 30 分钟后触发：抽取 SESSION_RECAP 并写入 memories 表 +
        session_summaries 表 + Chroma 索引（如启用）。"""
```

**集成路径**：
- `core/chat/repository.py::append_messages` 末尾追加 `self._memory.on_chat_message(...)` 钩子（fire-and-forget）
- `modules/p2p/tools/chat_history.py::search_my_chat_history` 工具委托给 `memory_manager.search_chat_history`
- TaskRegistry 后台 watcher 检测 idle session，调用 `on_session_idle`

### J5. 检索流程（精确优先 + 语义补充 + 时间衰减）

```
用户调用 @tool search_my_chat_history(query, days=30, limit=5)
  │
  └─ MemoryManager.search_chat_history()
       │
       ├─ Step 1: 解析 entity_ids
       │    └─ 优先用 unified_router 已抽取的实体（透传），
       │       缺省时正则提取（与 v1 entity.py 兼容）
       │
       ├─ Step 2: 构造时间窗
       │    └─ since = now_cn() - timedelta(days=days)
       │
       ├─ Step 3: 双通道检索（参考 v1 _dual_channel_search）
       │    │
       │    ├─ 通道 A（精确）：
       │    │    SELECT chat_sessions.*, session_entities.entities
       │    │    FROM chat_sessions JOIN session_entities ON session_id
       │    │    WHERE user_id=? AND deleted_at IS NULL
       │    │      AND updated_at >= since
       │    │      AND session_entities.entities @> ANY(entity_filters)
       │    │    ORDER BY updated_at DESC LIMIT limit*2
       │    │
       │    └─ 通道 B（语义补充，仅当 A 未填满）：
       │         Chroma chat_history.query(
       │             query_text=query,
       │             where={"user_id": user_id, "created_at_ge": since},
       │             n_results=limit*2,
       │         )
       │         去重（与 A 的 session_id 集合）
       │
       ├─ Step 4: 时间衰减 rerank（v1 未覆盖项，本次新增）
       │    final_score = base_score * exp(-λ * days_ago)
       │    λ = settings.memory.recency_decay_lambda（默认 0.02）
       │    （30 天前的得分约衰减到 55%）
       │
       └─ Step 5: 返回 top-limit
            [
              {
                "session_id": "...",
                "session_title": "...",
                "snippet": "...（200 字符）",
                "entities": {...},
                "created_at": "...",
                "relevance_score": 0.78,
                "match_type": "exact" | "semantic"
              },
              ...
            ]
```

**性能保护**：与 v1 long_term 检索一致，整体 200ms 超时。超时降级到只返回通道 A 结果（精确通道天然快，<10ms）。

### J6. 写入流程

#### J6.1 chat_messages 向量索引

```
ChatRepository.append_messages(session_id, messages)
  │
  ├─ [现有] 写入 chat_messages 表（同步）
  │
  └─ [J 新增] memory_manager.on_chat_message(...)
       │
       └─ ChatHistoryIndexer（MemoryManager 内部）
            │
            ├─ 入队（in-memory queue + 30s 延迟批处理）
            │  目的：按"对话片段"聚合（user→assistant 一对），
            │       减少向量化次数
            │
            └─ 批量 flush：
                 ├─ 取最近 30s 内的消息聚合成片段
                 ├─ 调用 embedding provider（dashscope）
                 ├─ 写入 Chroma chat_history collection
                 │   metadata = {user_id, session_id, message_ids,
                 │               role_pattern, created_at, entities}
                 └─ 失败重试 3 次后写入死信表 chat_index_dead_letter
```

**与 v1 的对齐**：
- 写入异步（哲学第 3 条）
- 失败不阻塞主流程（哲学第 1 条 + F14 兜底）
- 双 trace：在 memory trace 中记录 `memory.chat.index` span

#### J6.2 SESSION_RECAP 抽取

```
TaskRegistry idle watcher（每 5 分钟扫一次）
  │
  └─ for session_id in idle_sessions(threshold=30min):
       │
       └─ memory_manager.on_session_idle(session_id)
            │
            └─ SessionSummaryExtractor.extract(session_id)
                 │
                 ├─ 加载会话完整消息 + session_entities
                 ├─ 构造 prompt（llm_fast）：
                 │  "总结这段对话的核心主题、涉及实体、主要结论"
                 ├─ 解析结构化 JSON 输出：
                 │  {summary_text, key_entities, tags, conclusion}
                 ├─ 写 memories 表：
                 │  type=SESSION_RECAP, expires_at=now+90d
                 ├─ 写 session_summaries 表（结构化镜像）
                 └─ 同步写 Chroma（与 chat_history 同 collection）
```

**质量门槛**：消息数 < 4 不抽取（避免低质量摘要污染检索）；摘要 confidence < 0.6 不索引到 Chroma（仅写 PG）。

### J7. 数据模型

#### J7.1 MemoryType 枚举扩展

```python
# core/memory/types.py — v1 基础上新增
class MemoryType(StrEnum):
    USER_PREFERENCE = "user_preference"
    ENTITY_PROFILE = "entity_profile"
    ANALYSIS_INSIGHT = "analysis_insight"
    CORRECTION = "correction"
    DOMAIN_FACT = "domain_fact"
    SESSION_RECAP = "session_recap"   # ← 新增
```

#### J7.2 新增 session_summaries 表

```sql
CREATE TABLE session_summaries (
    session_id      VARCHAR(36)   PRIMARY KEY,
    user_id         VARCHAR(64)   NOT NULL,
    summary_text    TEXT          NOT NULL,
    key_entities    JSONB         NOT NULL DEFAULT '{}',
    tags            VARCHAR(64)[] NOT NULL DEFAULT '{}',
    analysis_count  INTEGER       NOT NULL DEFAULT 0,
    confidence      NUMERIC(3,2),
    memory_id       VARCHAR(36),                          -- 对应 memories 表的 SESSION_RECAP 行
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX session_summaries_user ON session_summaries (user_id, updated_at DESC);
CREATE INDEX session_summaries_tags ON session_summaries USING gin (tags);
```

**与 memories 表的关系**：
- memories 表存"自然语言摘要 + JSON attrs"（与 v1 一致），便于检索注入
- session_summaries 表存"结构化字段镜像"（便于 admin 查询、统计、调试）
- 两者通过 `memory_id` 关联

#### J7.3 Chroma collection 扩展

```yaml
chroma:
  collections:
    ontology_p2p: ontology_p2p
    business_docs_p2p: business_docs_p2p
    analysis_reports: analysis_reports
    memory_long_term: memory_long_term
    chat_history: chat_history          # ← 新增
```

**collection 设计**：
- 不分 chat_history vs session_recap 两个 collection，统一 chat_history
- 用 metadata `record_type` 区分：`chat_fragment` / `session_recap`
- 检索时按需过滤（search_chat_history 默认两种都召回）
- 季度分 collection（如 `chat_history_2026Q2`）只在数据量上规模后再做（Tier 2 后再评估）

#### J7.4 新增 chat_index_dead_letter 表（韧性）

```sql
CREATE TABLE chat_index_dead_letter (
    id              VARCHAR(36)   PRIMARY KEY,
    fragment_data   JSONB         NOT NULL,        -- 待索引的对话片段
    error_message   TEXT,
    retry_count     INTEGER       NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    last_retry_at   TIMESTAMPTZ
);
```

后台任务定时扫描死信表重试（与 v1 _VectorStoreProxy 退避机制配合）。

### J8. 工具集成

#### J8.1 新增工具：search_my_chat_history

```python
# modules/p2p/tools/chat_history.py（新增）

@tool
async def search_my_chat_history(
    query: str,
    days: int = 30,
    limit: int = 5,
) -> str:
    """搜索当前用户的历史对话内容，按相关度返回会话片段或会话摘要。

    适用场景：
    - 用户引用过去某段时间的对话（"上周那家供应商"、"上次的分析"、
      "之前讨论的付款政策"），需要找回历史会话上下文
    - 用户问"上个月在采购合规上得出过什么结论"等会话主题级回顾

    不适用场景：
    - 当前会话内的引用 → 走短期记忆（LangGraph checkpointer），不调本工具
    - 需要查询业务数据（PO/供应商/发票指标）→ 调 query_*/analyze_* 工具
    - 用户偏好查询 → 长期记忆 USER_PREFERENCE 自动注入，无需调用

    Args:
        query: 用户原始问题或主题关键词（自然语言）
        days: 时间窗口（天），1-365，默认 30。0 表示不限
        limit: 返回会话条数，1-10，默认 5

    Returns:
        JSON 字符串：[{session_id, session_title, snippet, entities,
        created_at, match_type, relevance_score}]，按相关度倒序。
        无匹配时返回 "[]"。可用于继续追问、实体消解、结论引用。
    """
    from core.memory import get_memory_manager
    manager = get_memory_manager()
    results = await manager.search_chat_history(
        user_id=_inject.get_current_user_id(),
        query=query,
        days=max(1, min(days, 365)) if days > 0 else None,
        limit=max(1, min(limit, 10)),
    )
    return _output._clip_and_dump(results)
```

#### J8.2 工具集注入策略

参考 v1 CLAUDE.md「工具集注入必须精准」原则：

- `search_my_chat_history` 仅在以下条件下注入 Agent 工具集：
  - `memory.chat_history.indexing_enabled = true` 且
  - 当前用户至少有 1 条 chat_history Chroma 索引（启动时检查或懒检查）
- RECALL 路径：unified_router 输出 `intent_kind="recall"` 时，工具集**只注入** `search_my_chat_history` + 必要的 query_* 工具，不注入 27 个全工具

#### J8.3 路由集成

v1 I 章已用 unified_router 替代了三级路由。RECALL 维度需要扩展：

```python
# core/orchestrator/unified_router.py — Prompt 扩展
"""
intent_kind 取值（在 v1 已有基础上扩展）：
- analysis        — 业务分析请求
- data_lookup     — 事实型查询
- recall          — 跨会话历史回查（"上周/上次/那家/之前讨论的"）
- clarification   — 需要追问澄清
- out_of_scope    — 不在 P2P 范围
- meta            — Agent 能力咨询
- chitchat        — 寒暄
"""
```

`intent_kind=recall` 时：
- Orchestrator 走 ReAct 路径，但工具集仅注入 `search_my_chat_history` + query_*
- LLM 必然先调 `search_my_chat_history` → 拿到结果后由 LLM 决策下一步

### J9. 可观测性（对齐 v1 双 trace 架构）

参考 v1 F13.0：所有 chat memory 操作的细粒度 span **写入 memory trace，不写 analyze trace**。analyze trace 仅记录粗粒度锚点。

#### J9.1 新增 memory trace span

| span 名称 | 触发点 | 关键属性 |
|-----------|--------|---------|
| `memory.chat.index.enqueue` | ChatHistoryIndexer 入队 | session_id, message_count, queue_depth |
| `memory.chat.index.flush` | ChatHistoryIndexer 批量写 Chroma | fragment_count, embedding_tokens, duration_ms, status |
| `memory.chat.index.dead_letter` | 重试 3 次失败 | session_id, error |
| `memory.chat.search` | MemoryManager.search_chat_history | user_id, query_len, entity_ids, channel_a_hits, channel_b_hits, recency_decay_applied, duration_ms |
| `memory.session_recap.extract` | SessionSummaryExtractor.extract | session_id, message_count, llm_tokens, confidence, duration_ms |
| `memory.session_recap.skip` | 抽取被质量门槛拒绝 | session_id, skip_reason |

#### J9.2 新增计数器

```
chat.index.enqueued        — 入队片段数
chat.index.flushed         — 成功写 Chroma 片段数
chat.index.dead_letter     — 死信片段数
chat.search.exact_hit      — 通道 A 命中次数
chat.search.semantic_hit   — 通道 B 命中次数
chat.search.timeout        — 检索超时次数
session_recap.extracted    — SESSION_RECAP 抽取成功次数
session_recap.skipped      — 跳过抽取次数
```

#### J9.3 admin metrics 扩展

`api/routes/admin_metrics.py` 新增端点：
- `GET /admin/metrics/chat-index-status` — 索引覆盖率（已索引片段数 / 总 chat_messages 片段数）
- `GET /admin/metrics/chat-search-quality` — 检索命中率分布（exact / semantic / 0 hit）
- `GET /admin/metrics/session-recap-coverage` — 会话覆盖率（已摘要会话数 / 总会话数）

### J10. 配置项扩展

在 v1 G15 基础上新增以下配置块（对齐生产标准 / 默认安全可关）：

```yaml
memory:
  # ── v1 已有配置保留不变 ──

  # [J 新增] 跨会话对话记忆配置
  chat_history:
    indexing_enabled: true              # chat 消息异步向量索引总开关
    fragment_aggregation_seconds: 30    # 入队后多久 flush 一次（聚合对话片段）
    fragment_max_messages: 6            # 单片段最多包含消息数
    embed_provider: ""                  # 空表示沿用 chroma.embedding_provider
    max_per_user_fragments: 5000        # 每用户最多保留片段数（cap 淘汰，与 long_term cap 协同）
    search_timeout_seconds: 0.2         # search_chat_history 同步超时（与 long_term 一致）
    search_default_limit: 5
    search_max_limit: 10
    search_default_days: 30
    search_max_days: 365
    dead_letter_retry_max: 3            # 死信表最大重试次数
    dead_letter_backoff_seconds: 60     # 死信重试退避

  # [J 新增] 会话摘要（SESSION_RECAP）配置
  session_recap:
    enabled: true                       # SESSION_RECAP 抽取总开关
    idle_threshold_seconds: 1800        # 会话不活跃多久后触发摘要（30 min）
    min_messages: 4                     # 消息数下限（低于此值不抽取，避免污染检索）
    min_confidence: 0.6                 # 抽取 confidence 下限（低于此值仅写 PG，不写 Chroma）
    summary_max_chars: 500              # 摘要文本上限
    summary_timeout_seconds: 30         # llm_fast 调用超时
    watcher_interval_seconds: 300       # TaskRegistry idle watcher 扫描间隔（5 min）

  # [J 新增] 时间衰减检索（v1 未覆盖项）
  recency_decay:
    enabled: true                       # 时间衰减开关
    lambda: 0.02                        # exp(-λ * days_ago)；30 天前得分约 55%
    apply_to:                           # 仅对以下类型应用衰减
      - session_recap
      - analysis_insight                # ← v1 修订项（见 §4.1）

  # [J 新增] TTL 配置扩展
  ttl:
    # ... v1 已有 5 项保留 ...
    session_recap_days: 90              # SESSION_RECAP 硬过期

  # [J 新增] long_term 类型预算扩展（6 类）
  long_term:
    type_budget_pct:
      entity_profile: 35                # 40 → 35（让 5 个百分点给 SESSION_RECAP）
      user_preference: 20
      analysis_insight: 15
      correction: 15
      domain_fact: 10
      session_recap: 5                  # ← 新增（小预算，仅在 RECALL 路径放大）
    type_max_retrieved:
      session_recap: 3                  # ← 新增
      # ... 其它沿用 v1 ...
```

**配置项设计原则（与 v1 对齐）**：
- 所有新配置都有合理生产默认值
- 关键行为可关（`indexing_enabled` / `session_recap.enabled` / `recency_decay.enabled`）
- 资源上限有硬限制（`max_per_user_fragments` / `dead_letter_retry_max`）
- 超时显式（`search_timeout_seconds` / `summary_timeout_seconds`）

### J11. 降级与韧性（扩展 v1 F14 表）

| 环节 | 失败场景 | 降级行为 | 日志级别 |
|------|---------|---------|---------|
| **chat 索引入队** | ChatHistoryIndexer 异常 | 跳过本次入队，chat_messages 已写 PG，不影响主流程 | WARNING |
| **chat 索引 flush** | embedding 调用超时/失败 | 重试 3 次后写死信表，不影响 ChatRepository | WARNING |
| **chat 索引 flush** | Chroma 写入失败 | 写死信表 | WARNING |
| **chat history 检索** | 通道 A SQL 超时 | 跳过通道 A 直接走通道 B | WARNING |
| **chat history 检索** | 通道 B Chroma 不可用 | 仅返回通道 A 结果（精确通道天然快） | WARNING |
| **chat history 检索** | 整体 200ms 超时 | 返回空列表，工具回复 "[]"，Agent 走兜底 | WARNING |
| **SESSION_RECAP 抽取** | llm_fast 超时 | 跳过本会话摘要，下次 idle watcher 重试 | WARNING |
| **SESSION_RECAP 抽取** | confidence < 0.6 | 仅写 PG（结构化镜像），不写 Chroma | INFO |
| **死信重试** | 第 N 次重试仍失败 | 死信记录保留，等待人工介入 | ERROR（仅触达 max 时） |
| **idle watcher** | TaskRegistry 异常 | 不影响主流程，下次启动恢复 | WARNING |
| **时间衰减计算** | math.exp 异常（理论不发生） | 退化为 1.0（不衰减） | WARNING |
| **MemoryManager.search_chat_history** | 任意未捕获异常 | 返回空列表 | WARNING |

**核心原则（与 v1 哲学第 1 条对齐）**：chat memory 任何环节失败都**不阻塞** v1 已有的 5 类记忆 + 短期记忆 + 分析主流程。chat memory 是"锦上添花的锦上添花"。

## 4. 对 v1 设计的修订点（J 章之外的零散修订）

除 J 章新增内容外，本次 refresh 对 v1 设计做以下修订。所有修订都是**对齐性增强**，不破坏 v1 已实施部分。

### 4.1 ANALYSIS_INSIGHT 启用时间衰减（v1 E12 增强）

**v1 现状**：ANALYSIS_INSIGHT TTL 60 天硬过期，时间维度仅作过滤不作权重。
**问题**：60 天内的 insight 检索得分等同 — 但用户问"近期趋势" vs "上次提到" 时新旧 insight 价值差异很大。
**修订**：ANALYSIS_INSIGHT 也纳入 `memory.recency_decay.apply_to`（同 SESSION_RECAP）。
**影响**：仅检索阶段的 score 计算变化，写入路径不变；TTL 仍保留作为兜底过滤。
**实施位置**：`core/memory/long_term.py::_dual_channel_search` 末尾新增 rerank 步骤。

### 4.2 MemoryType 从 5 类扩到 6 类（v1 B3 修订）

**v1 现状**：5 种封闭枚举 — `user_preference / entity_profile / analysis_insight / correction / domain_fact`。
**修订**：新增 `session_recap`（详见 J7.1）。封闭枚举的扩展方式与 v1 完全一致 — 修改 `core/memory/types.py` + 配置 + 迁移。
**与 v1 设计的契合**：v1 B3 明确"扩展类型需修改代码 + 配置"，本次符合规则。
**type_budget_pct 重新分配**（参 J10）：从 entity_profile 让出 5%，给 session_recap，避免 long_term 总预算膨胀。

### 4.3 双 trace 架构在 chat 维度的扩展（v1 F13 增强）

**v1 现状**：F13 列了 11 个 memory trace span，仅覆盖 5 类记忆 + 整合 + 摘要。
**修订**：新增 6 个 chat 维度 span（详见 J9.1），全部写入 memory trace（不写 analyze trace）。
**关联方式**：与 v1 一致 — analyze trace 的 `memory_ref` span 通过 `memory_trace_id` 关联到 memory trace；新增的 chat span 都共享同一 mem_trace_id。
**admin 视图**：`/admin/metrics` 新增 3 个 chat / session_recap 维度端点（详见 J9.3）。

### 4.4 MemoryManager 接口从 5 方法扩到 7 方法（v1 A1.1 修订）

**v1 现状**：MemoryManager 暴露 5 个方法 — `build_context / on_analysis_complete / on_user_message / load_session / save_session_entities`。
**修订**：新增 2 个方法（详见 J4）：
- `search_chat_history(user_id, query, ...)` — 同步带超时，被 `@tool search_my_chat_history` 调用
- `on_chat_message(session_id, message_id, role, content)` — fire-and-forget 索引钩子
- 内部新增 `on_session_idle(session_id)` — 由 TaskRegistry 后台调用，不暴露给 Orchestrator
**契合度**：与 v1 的"MemoryManager 是单一入口"原则完全一致；不引入第二个外部入口。

### 4.5 路由 RECALL 维度的实现位置（v1 I 章修订）

**v1 现状**：v1 I 章（统一 LLM 路由）落地后，RECALL 仍走早期 bypass 路径（`router/__init__.py:47` 正则匹配）。
**修订建议**：把 RECALL 识别移交 unified_router LLM（见 J8.3 prompt 扩展）。
- 优点：与 I 章"统一 LLM 调用"原则对齐，正则规则不再补丁式扩张
- 代价：RECALL 多走一次 LLM 调用（但 unified_router 本来就要调 LLM，无增量成本）
- 现状的 bypass 路径可保留作为 fast-path 兜底（明确正则 + 短查询时直接 bypass，省 LLM token）
**实施时机**：与 J 章工具落地同步实施，避免出现"工具已注入但路由不知道用"的中间态。

## 5. 死代码清理建议

### 5.1 `param_extractor.py` 死代码遗留

**事实**：
- v1 设计 I4 章明确要求"删除 `core/orchestrator/param_extractor.py` — 合并进统一 LLM 调用"
- 现状：
  - `core/orchestrator/param_extractor.py`（包含 `ParamExtractor` 类，~200 行）仍存在
  - `tests/unit/test_param_extractor.py`（~20 个测试用例）仍存在
  - `core/orchestrator/orchestrator.py:160` 注释明确："ParamExtractor 已合并到统一 LLM 路由（UnifiedRouter），不再需要"
  - 全代码库 grep 验证：除自身定义、自身测试、unified_router 文档字符串提及、orchestrator 注释提及外，**无任何代码路径调用**

**性质**：与已修复的技术债 #10（L1/L2 死配置）完全同级 — "已注释为完成但实际遗留"。

**处置**（用户已确认 R3=A）：登记为正式技术债 **#15**：
- `docs/agent_issue.md` 追加条目 #15
- `core/orchestrator/param_extractor.py` 顶部加 `# TECH-DEBT(#15)` 注释（双记录）
- `tests/unit/test_param_extractor.py` 顶部加 `# TECH-DEBT(#15)` 注释（同步标记）
- 修复方式：删除两个文件 + 同步移除注释 + agent_issue.md 条目移到"已修复"区
- 修复时机：可独立于本次 J 章工作进行；等下次小修复批次顺手处理

详见 [agent_issue.md](../agent_issue.md) 条目 #15。

## 6. 实施评估

### 6.1 改动文件清单

#### 新增文件（7 个）

| 文件 | 职责 | 代码量估算 |
|------|------|-----------|
| `core/memory/chat_indexer.py` | ChatHistoryIndexer：消息片段聚合 + 异步 embedding + Chroma 写入 + 死信表 | ~250 行 |
| `core/memory/session_summary.py` | SessionSummaryExtractor：llm_fast 抽取会话摘要 | ~180 行 |
| `core/memory/recency.py` | 时间衰减权重计算（exp decay） | ~40 行 |
| `modules/p2p/tools/chat_history.py` | `@tool search_my_chat_history` | ~80 行 |
| `migrations/versions/0014_session_recap_and_chat_index.py` | DDL：MemoryType 枚举扩展、session_summaries 表、chat_index_dead_letter 表 | ~120 行 |
| `scripts/backfill_chat_index.py` | 历史 chat_messages 回填 Chroma 索引 | ~100 行 |
| `tests/unit/test_chat_indexer.py` + 其它 4 个测试文件 | 见 6.2 测试清单 | ~600 行 |

#### 改造文件（8 个）

| 文件 | 改动内容 | 改动量估算 |
|------|---------|-----------|
| `core/memory/types.py` | `MemoryType` 新增 `SESSION_RECAP` | ~5 行 |
| `core/memory/manager.py` | 新增 `search_chat_history` / `on_chat_message` / `on_session_idle` 三方法 + 持有两个内部组件 | ~150 行 |
| `core/memory/long_term.py` | `_dual_channel_search` 末尾加 recency rerank；`type_budget_pct` 扩展到 6 类 | ~50 行 |
| `core/memory/tables.py` | 新增 `session_summaries_table` + `chat_index_dead_letter_table` | ~50 行 |
| `core/memory/injection.py` | `format_memory_injection` 新增 `[相关历史会话回顾]` 区块 | ~25 行 |
| `core/chat/repository.py` | `append_messages` 末尾追加 `memory_manager.on_chat_message` 钩子 | ~10 行 |
| `core/orchestrator/unified_router.py` | Prompt 扩展 `intent_kind="recall"` + RECALL 工具集精简注入 | ~30 行 |
| `modules/p2p/tools/__init__.py` + `provider.py` + `prompts.py` | 工具注册 + 速查表 + RECALL 场景示例 | ~30 行 |
| `core/tasks/registry.py` | idle session watcher（5 分钟扫一次） | ~50 行 |
| `config/settings.py` + `config/config.yaml` | 新增 `chat_history` / `session_recap` / `recency_decay` 配置块 | ~80 行 |
| `api/routes/admin_metrics.py` | 新增 3 个 chat / recap 端点 | ~60 行 |

#### DB 迁移（1 个）

`0014_session_recap_and_chat_index.py`：MemoryType 应用层枚举扩展（无 DB 约束变更）+ `session_summaries` 表 + `chat_index_dead_letter` 表 + 索引

### 6.2 测试清单

| 文件 | 覆盖范围 |
|------|---------|
| `tests/unit/test_chat_indexer.py` | 入队 / flush / 死信 / 失败重试 / 配置开关 |
| `tests/unit/test_session_summary.py` | 摘要抽取 / confidence 门槛 / idle 检测 |
| `tests/unit/test_recency_decay.py` | 衰减算法 / 边界值 / 配置控制 |
| `tests/unit/test_chat_history_tool.py` | 工具入参校验 / 委托 MemoryManager / 输出截断 |
| `tests/integration/test_recall_e2e.py` | 跨 session 召回端到端：构造 2 个 session → 第二个引用第一个 → Agent 找到 |

### 6.3 工作量与风险

| 维度 | 评估 |
|------|------|
| **总代码量** | 新增 ~770 行 + 改造 ~535 行 + 测试 ~600 行 ≈ **1900 行** |
| **工作量** | 约 **8-10 个工作日**（含测试 + 迁移 + 联调 + 历史回填） |
| **风险等级** | **中等** |

**主要风险：**

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Chroma 集合膨胀（chat_history 数据量大） | 检索变慢 | `max_per_user_fragments=5000` 硬上限 + 季度分 collection 留作 V2 优化 |
| embedding 成本（每片段一次 API） | LLM 账单 | `dashscope` provider + `fragment_aggregation_seconds=30` 聚合 + 用户级限流（沿用现有限流） |
| SESSION_RECAP 与 ANALYSIS_INSIGHT 边界混淆 | 检索注入冗余 | 通过抽取 prompt 严格区分 + 检索 type_budget_pct 强制隔离 |
| 用户隐私（chat 内容向量化） | 合规风险 | 配置开关 `indexing_enabled` 默认开但可关；PII 脱敏作为 Tier 3 工程债跟踪（见 enhancement_gaps §3.3） |
| RECALL 路由迁移到 unified_router | 误判增加 | 保留现有 bypass 正则作为 fast-path 兜底 |
| 历史回填卡住 | 阻塞回填 | 分批 + 断点续传 + 手动控制开关 |

### 6.4 与 v1 H18 对接的实施 Phase

v1 H18 已规划 Phase 1-6（Phase 1-6 已全部落地）。本次 J 章新增 Phase 7-9。

#### Phase 7：chat 索引基础设施（3 天）

- 新增 `core/memory/chat_indexer.py`、`recency.py`
- 新增 migration `0014`
- `core/memory/manager.py` 加 `on_chat_message`
- `core/chat/repository.py::append_messages` 钩子
- 历史回填脚本
- 测试：test_chat_indexer.py
- **验收**：新 chat 消息自动索引到 Chroma；历史可回填；索引失败进死信表

#### Phase 8：search_chat_history 工具 + 路由（3 天）

- 新增 `modules/p2p/tools/chat_history.py`
- `MemoryManager.search_chat_history` 实现（双通道 + 时间衰减）
- 工具集注入 + prompts 更新
- `unified_router.py` 新增 `intent_kind="recall"`
- 测试：test_chat_history_tool.py + test_recall_e2e.py
- **验收**：用户在新 session 引用旧 session → Agent 调用 search_my_chat_history → 找到正确历史会话

#### Phase 9：SESSION_RECAP + idle watcher（2 天）

- 新增 `core/memory/session_summary.py`
- `MemoryManager.on_session_idle`
- TaskRegistry idle watcher
- `MemoryType.SESSION_RECAP` 写入注入
- `injection.py` 新增 `[相关历史会话回顾]` 区块
- 测试：test_session_summary.py
- admin metrics 端点
- **验收**：30 分钟不活跃会话自动摘要；用户问"上个月在采购合规上得出过什么结论" → 召回 SESSION_RECAP 注入分析

**总工期**：8-10 天（含 #15 死代码清理）。

## 7. 待用户确认的决策点

实施前请确认以下决策：

1. **Phase 7-9 节奏**：是否同意按 7 → 8 → 9 顺序推进？Phase 7 完成可独立验证（数据流通），Phase 8 是用户感知关键节点（工具可用），Phase 9 是质量提升。如希望先快速验证用户价值，可考虑 7 + 8 先做，9 延后观察。

2. **chat_history 索引默认开关**：建议默认 **开启**（用户体验优先），但提供 `memory.chat_history.indexing_enabled` 配置项可关。是否同意？

3. **历史数据回填**：是否在 Phase 7 阶段一并把现有 chat_messages 全量回填到 Chroma？还是只索引新数据先观察规模？建议**先索引新数据**，回填脚本作为可选工具（避免一开始就给 embedding API 灌洪水）。

4. **MemoryType 扩展实施风险**：从 5 类扩到 6 类需要修改 `type_budget_pct` 默认值（v1 entity_profile 40 → 35，给 session_recap 5）。是否同意这个预算分配？或希望保留 entity_profile 40 + session_recap 单独占用更小预算？

5. **#15 死代码清理时机**：是否希望本次 refresh 工作中**顺手删除** `param_extractor.py` + `test_param_extractor.py`？还是登记 #15 后由后续小修复批次处理？建议后者（与本次 J 章工作隔离，降低 PR 复杂度）。

6. **RECALL 路由迁移**：是否同意把 RECALL 识别从 bypass 正则迁到 unified_router LLM？保留 bypass fast-path 作为兜底。如不同意，工具仍可挂在 bypass 路径下使用。

7. **金标用例标注**：Phase 8 的 `test_recall_e2e.py` 需要 5-10 条 golden 用例标注用户跨 session 引用场景。建议项目方提供 3-5 条业务场景，工程侧扩展到 10 条。

8. **是否立刻开始实施**？还是先针对本设计做进一步评审？

确认后即可按 Phase 7 → Phase 9 严格推进。
