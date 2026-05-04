# 长期记忆模块重构总结

## 背景

`long_term_issue.md` 记录了长期记忆模块的 12 个设计问题。本文档记录各问题的处理结论、改动范围和当前状态。

---

## 问题处理状态

| # | 问题 | 状态 | 处理方式 |
|---|------|------|---------|
| 1 | 总开关缺失 | ✅ 已处理 | `memory.long_term_enabled` 字段已在 settings 中存在，agent 两处 gate 已实现 |
| 2 | LIKE 召回几乎不命中 | ✅ 已处理 | 改为 PG FTS（`tsvector` + GIN 索引）+ Chroma 向量召回，应用层 RRF 融合 |
| 3 | 无上限 / 无淘汰 | ✅ 已处理 | `max_per_user` 滚动 cap，超出按 `created_at` 两步法删除，同步清理向量库 |
| 4 | 写入策略粗放 | ✅ 已处理 | 三层过滤下沉到 `MemoryRepository.save()`：L1 长度、L2 内容指纹去重、L3 空结论过滤 |
| 5 | 写入内容结构化不足 | ✅ 已处理 | agent 写入时携带 `analysis_type / anomaly_count / time_range / entities` 到 metadata |
| 6 | 双轨召回未去重未排序 | ✅ 已处理 | 两路候选 id 经 RRF（Reciprocal Rank Fusion）融合后截断，回表取完整行 |
| 7 | 自建 engine 不复用统一数据库层 | ✅ 已处理 | `__init__` 改为接收 `engine` 注入，单例由 `get_engine(settings.postgresql)` 统一提供 |
| 8 | `metadata` 列名反模式 | ✅ 已处理 | 列名改为 `attrs`，alembic 迁移已添加 |
| 9 | 索引策略不匹配查询形态 | ✅ 已处理 | 加复合索引 `(user_id, created_at DESC)` 和 GIN FTS 索引 `memories_content_fts` |
| 10 | 异常全静默 | ✅ 已处理 | 所有 `except Exception: pass` 替换为 WARNING 日志 + `_inc()` 计数器 |
| 11 | reports 与 memories 职责混杂 | ✅ 已处理 | 拆分为 `MemoryRepository` + `ReportRepository`，`LongTermMemory` 改为 facade |
| 12 | 单例 + vector_store 永久降级 | ✅ 已处理 | 引入 `_VectorStoreProxy`，指数退避重试（初始 60s，上限 3600s），线程安全 |

---

## 核心架构变化

### 改动前

```
LongTermMemory（单一大类）
├── memories 操作（CRUD + LIKE 召回）
├── reports 操作（CRUD）
├── 自建 engine（独立连接池）
├── vector_store（一次初始化，失败永久 None）
└── 异常全静默（except: pass）
```

### 改动后

```
MemoryRepository          ReportRepository
├── save()（三层过滤）    ├── save()
├── search()（Hybrid）    ├── get()
├── search_semantic()     ├── list()
├── _enforce_user_cap()   ├── search_semantic()
└── delete_user/all()     └── delete_user/all()
         │                         │
         └─────── 共享 engine ─────┘
                      │
              _VectorStoreProxy（懒重试代理）
                      │
              factory（按退避重试）

LongTermMemory（Facade，向后兼容）
├── _mem_repo: MemoryRepository
└── _rep_repo: ReportRepository
```

---

## 关键设计决策

### Hybrid 检索（问题 2 + 6）

- **稀疏路**：PG FTS `to_tsvector('simple', content) @@ to_tsquery`，`ts_rank` 排序；SQLite 退化为 `LIKE`（仅测试）
- **稠密路**：Chroma 向量检索，`user_id + source=memory` 过滤
- **融合**：两路各取 `limit*4` 候选 id，`_rrf_fuse()` 按 RRF 公式融合，截断到 `limit`，回表取完整行
- **失败模式**：任一路挂掉不影响另一路，两路都失败返回 `[]`

### 三层写入过滤（问题 3 + 4）

| 层 | 触发条件 | 配置项 | 默认值 |
|----|---------|--------|--------|
| L1 内容长度 | `len(content.strip()) < min_content_len` | `long_term_min_content_len` | 50 |
| L2 内容指纹去重 | 同 `user_id` + 同 `content_hash` 在窗口内已存在 | `long_term_dedupe_window_seconds` | 900 |
| L3 空结论过滤 | `anomaly_count==0` 且 `summary` 为空（需开启） | `long_term_skip_empty_conclusions` | False |

- `content_hash`：`sha256(normalize(content))[:16]`，normalize 折叠空白 + 转小写

### _VectorStoreProxy（问题 12）

```python
proxy = _VectorStoreProxy(factory=_make_vs_factory(settings, "memory_long_term"))
vs = proxy.get()   # 自动按退避重试，返回实例或 None
proxy.invalidate() # 强制失效，下次 get() 立即重试
```

- 初始退避 60s，每次失败翻倍，上限 3600s
- 成功恢复后退避重置为 60s
- 线程安全（`threading.Lock`）

### LongTermMemory Facade（问题 11）

现有调用方（agent、API 路由）零改动，`LongTermMemory` 保留全部公开方法名，方法体为 1-3 行委托调用：

```python
def save_memory(self, ...) -> str | None:
    return self._mem_repo.save(...)

def get_report(self, report_id: str) -> dict | None:
    return self._rep_repo.get(report_id)
```

---

## 可观测性

模块级计数器（`_counters`，线程安全），可通过 `get_counters()` 查询：

| 计数器 | 含义 |
|--------|------|
| `ltm.save.written` | 成功写入条数 |
| `ltm.save.skipped.too_short` | L1 过滤跳过 |
| `ltm.save.skipped.duplicate` | L2 去重跳过 |
| `ltm.save.skipped.empty_conclusion` | L3 过滤跳过 |
| `ltm.save.error` | 写入失败（DB 异常） |
| `ltm.dedupe.error` | 去重查询失败 |
| `ltm.enforce_cap.error` | cap 淘汰失败 |
| `ltm.search.error` | 稀疏检索失败 |
| `ltm.vector.write.error` | 向量写入失败 |
| `ltm.vector.search.error` | 向量检索失败 |
| `ltm.vector.delete.error` | 向量删除失败 |

所有异常路径均有 WARNING 以上日志，格式为结构化 key=value。

---

## 测试覆盖

`tests/unit/test_long_term_memory.py`，83 个测试用例：

| 测试类 | 覆盖场景 |
|--------|---------|
| `TestLongTermMemoryInit` | init_tables 幂等 |
| `TestMemoryOperations` | CRUD、limit、user 隔离 |
| `TestReportOperations` | CRUD、list、limit |
| `TestLongTermMemoryWithVectorStore` | 向量旁路、hybrid 召回、错误吞掉 |
| `TestUserCap` | cap 淘汰、向量同步删除、user 隔离 |
| `TestWriteFilters` | L1/L2/L3 三层过滤、边界值 |
| `TestNormalizationAndHash` | normalize/hash 单元 |
| `TestRRFFusion` | RRF 融合、tsquery 工具 |
| `TestMemoryRepository` | 新 repo 类独立测试 |
| `TestReportRepository` | 新 repo 类独立测试 |
| `TestVectorStoreProxy` | 退避、恢复、失效、集成写入 |

---

## 文件变更清单

| 文件 | 变更类型 |
|------|---------|
| `core/memory/long_term.py` | 重构（拆分三类 + proxy + 可观测性） |
| `core/memory/__init__.py` | 新增导出 `MemoryRepository`、`ReportRepository`、`get_memory_repository`、`get_report_repository`、`reset_repositories` |
| `core/memory/tables.py` | `metadata` 列改名 `attrs`，新增 `content_hash` 列和复合索引 |
| `tests/unit/test_long_term_memory.py` | 大幅扩充（从 ~50 个用例到 83 个） |
