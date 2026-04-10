# 长期记忆设计问题分析与优化方案

## 背景

本项目（eragent）的长期记忆模块位于 [core/memory/long_term.py](../core/memory/long_term.py)，基于 PostgreSQL 存储 `memories` 与 `reports` 两张表，供 P2P Agent 在每轮 `analyze` 时按 `user_id` 召回历史片段注入 prompt，并将本轮结论写回。当前实现存在若干设计与工程层面的缺陷，影响召回命中率、数据膨胀、可控性与可观测性。

本文档逐题分析现状、根因、影响与解决方案，按优先级给出改动建议。

## 方法

- 只读方式阅读 long_term 相关源码与调用方，不含 short_term 相关讨论。
- 每个问题按 **现象 → 证据 → 根因 → 影响 → 方案（P0/P1/P2）→ 验证** 结构展开。
- 方案分最小改动（P0，先止血）与更彻底的改造（P1/P2）。

## 目录

1. [长期记忆总开关缺失](#问题-1长期记忆总开关缺失)
2. [LIKE 召回几乎无法命中](#问题-2like-召回几乎无法命中)
3. [长期记忆数量无上限 / 无淘汰](#问题-3长期记忆数量无上限--无淘汰)
4. [写入策略过于粗放](#问题-4写入策略过于粗放)
5. [写入内容结构化不足](#问题-5写入内容结构化不足)
6. [双轨召回未去重 / 未统一排序](#问题-6双轨召回未去重--未统一排序)
7. [自建 engine 未复用统一数据库层](#问题-7自建-engine-未复用统一数据库层)
8. [`metadata` 列名反模式](#问题-8metadata-列名反模式)
9. [索引策略不匹配查询形态](#问题-9索引策略不匹配查询形态)
10. [异常全静默，无可观测性](#问题-10异常全静默无可观测性)
11. [reports 与 memories 职责混杂](#问题-11reports-与-memories-职责混杂)
12. [单例 + vector_store 永久降级](#问题-12单例--vector_store-永久降级)

---

## 问题 1：长期记忆总开关缺失

### 现象

用户/运维**无法整体关闭长期记忆读写**。即使在离线调试、回归测试、多租户演示、隐私合规等场景下希望"这次会话不要读也不要写长期记忆"，当前实现也没有任何配置项可以达成这一点——只要 `get_long_term_memory()` 能连上 Postgres，读写就会发生。

### 证据

1. `MemorySettings` 只有数量类字段，没有 enabled 开关：

   [config/settings.py:237-244](../config/settings.py#L237-L244)
   ```python
   class MemorySettings(BaseSettings):
       short_term_max_messages: int = 20
       short_term_summary_threshold: int = 15
       long_term_max_retrieved: int = 5
   ```

2. `config.yaml` 的 `memory.long_term` 块同样只有 `max_retrieved`：

   [config/config.yaml:100-101](../config/config.yaml#L100-L101)
   ```yaml
   memory:
     long_term:
       max_retrieved: 5
   ```

3. P2P Agent 在 `analyze()` 中**无条件**读写长期记忆，没有任何配置判断：

   [modules/p2p/agent.py:317-333](../modules/p2p/agent.py#L317-L333)
   ```python
   try:
       from core.memory import get_long_term_memory
       ltm = get_long_term_memory()
       max_retrieved = self._settings.memory.long_term_max_retrieved
       long_term_snippets = ltm.search_memories(
           user_id=user_id, query=query, limit=max_retrieved
       )
       semantic_hits = ltm.search_memories_semantic(...)
   ```

   [modules/p2p/agent.py:375-388](../modules/p2p/agent.py#L375-L388)
   ```python
   if content:
       try:
           from core.memory import get_long_term_memory
           ltm = get_long_term_memory()
           ltm.save_memory(...)
   ```

4. 现有的 `chroma.enable_long_term_indexing` / `enable_report_indexing` 只控制**是否旁路写入向量库**，不控制 SQL 主路径：

   [config/settings.py:91-93](../config/settings.py#L91-L93)
   ```python
   # 是否在长期记忆 / 报告写入时同步索引到向量库（默认关闭，按需启用）
   enable_long_term_indexing: bool = False
   enable_report_indexing: bool = False
   ```

### 根因

- 设计阶段将"长期记忆"与"向量索引"两件事混为一个开关语义，实际上它们是**两层正交能力**：
  - 主路径：SQL 层的 `memories` 读写（能力本身）。
  - 旁路：向量索引（召回质量增强手段）。
- `chroma.enable_long_term_indexing` 只守住了旁路，主路径无人守护，导致"关不掉"。
- 调用方 `P2PAgent.analyze` 直接依赖模块级单例 `get_long_term_memory()`，没有在 Settings 层面做 gate。

### 影响

- **测试/CI**：单元测试必须显式 mock 或连到测试库，否则写入真实 Postgres，污染数据。
- **隐私与合规**：无法针对敏感会话/租户一键关闭记忆写入。
- **故障隔离**：长期记忆表出问题（锁、膨胀、DDL 变更）时，没有"软开关"让 Agent 快速绕过，只能靠异常静默降级（见问题 10）。
- **调试体验**：复现问题时想排除"长期记忆注入历史片段"这一变量，必须改代码或清表。

### 方案

#### P0：新增 `MemorySettings.long_term_enabled` 开关（默认 True）

在 `MemorySettings` 增加字段（默认 True，保持现有行为向后兼容）：

```python
class MemorySettings(BaseSettings):
    short_term_max_messages: int = 20
    short_term_summary_threshold: int = 15
    long_term_enabled: bool = True          # 新增：长期记忆主路径总开关
    long_term_max_retrieved: int = 5
```

`config.yaml` 对应补字段，`from_yaml` 扁平化时读取 `memory.long_term.enabled`：

```yaml
memory:
  long_term:
    enabled: true
    max_retrieved: 5
```

```python
merged["memory"] = {
    "short_term_max_messages": short_term.get("max_messages", 20),
    "short_term_summary_threshold": short_term.get("summary_threshold", 15),
    "long_term_enabled": long_term.get("enabled", True),
    "long_term_max_retrieved": long_term.get("max_retrieved", 5),
}
```

`P2PAgent.analyze` 读写两处都加 gate：

```python
if self._settings.memory.long_term_enabled:
    try:
        ltm = get_long_term_memory()
        long_term_snippets = ltm.search_memories(...)
        ...
    except Exception as exc:
        _logger.debug("long-term memory retrieval skipped: %s", exc)
```

写入同理。关闭时完全不触达 `get_long_term_memory()`，避免懒单例初始化。

#### P1：开关语义分层

把"长期记忆开关"明确拆成三级，避免未来再混淆：

| 层级 | 开关 | 控制对象 | 默认 |
|------|------|----------|------|
| 主路径 | `memory.long_term.enabled` | SQL 读写 memories | True |
| 旁路写 | `chroma.enable_long_term_indexing` | 写入时同步向量库 | False |
| 旁路读 | （建议新增）`memory.long_term.semantic_enabled` | 召回时调用 `search_memories_semantic` | 跟随 `enable_long_term_indexing` |

当前 `search_memories_semantic` 的触发条件是"构造时 vector_store 是否存在"，实际上耦合了写旁路开关，语义混乱。拆开后，读侧可以独立启停。

#### P2：把 gate 下沉到 `LongTermMemory` 自身

在 `LongTermMemory.__init__` 接收 `enabled: bool`，所有 `save_*/search_*` 方法在 `enabled=False` 时直接 return 空结果/空 id，调用方无需重复判断。好处：

- 单元测试可以直接 `LongTermMemory(dsn=..., enabled=False)`，无需 mock。
- 未来新增调用方（如 orchestrator 层）不会遗漏 gate。

### 验证

- 单测：`MemorySettings(long_term_enabled=False)` 下，`P2PAgent.analyze` 不应调用 `save_memory` / `search_memories`（用 monkeypatch 计数或 spy）。
- 配置回归：`config.yaml` 不配置 `enabled` 字段时，默认仍为 True，现有行为不变。
- 集成：端到端测试中临时关闭开关，确认 `memories` 表没有新行写入，同时 agent 流程仍可正常返回结果。
