# 测试缺陷登记表

记录所有在生产/手动测试中发现但现有自动化测试未覆盖的缺陷。每条缺陷必须审视根因并提出测试补充方案。

## 登记格式

```
### TD-<编号>: <标题>
- **发现日期**: YYYY-MM-DD
- **严重程度**: P0(阻塞)/P1(重要)/P2(一般)/P3(低)
- **影响范围**: <受影响的模块/功能>
- **现象**: <用户可观测到的错误行为>
- **根因**: <技术根因简述>
- **代码位置**: `<file>:<line>`
- **修复状态**: 未修复 / 已修复(<commit-hash>)
- **测试缺口分析**:
  - 为什么现有测试未覆盖: <原因>
  - 建议补充的测试场景: <描述>
  - 测试类型: 单元测试 / 集成测试 / 端到端测试
```

---

## 活跃缺陷

### TD-001: 长期记忆 dense 检索缺少 memory_type 过滤导致跨类型污染
- **发现日期**: 2026-05-04
- **严重程度**: P1
- **影响范围**: core/memory — 所有使用 `_search_by_type_hybrid` 的记忆召回路径
- **现象**: 新 session 分析报告中出现旧 session 的错误结论（如"物料信息缺失"），用户换 session 无法消除
- **根因**: `_dense_search_ids` 向量检索 where 条件仅含 `user_id` + `source`，未传入 `memory_type`，导致 `_search_by_type_hybrid` 的 RRF 融合混入其他类型记忆
- **代码位置**: `core/memory/long_term.py:727-730`
- **修复状态**: 已修复（当前分支未提交）
- **测试缺口分析**:
  - 为什么现有测试未覆盖: 单元测试中 vector store 使用 mock，mock 的 `search()` 不验证 where 条件中是否包含 `memory_type` 字段；集成测试未构造跨类型记忆数据来验证隔离性
  - 建议补充的测试场景: 写入多种 memory_type 记忆后，调用 `_search_by_type_hybrid` 验证返回结果仅包含指定类型；构造跨 session 记忆验证新 session 不被旧 insight 污染
  - 测试类型: 集成测试（需真实 vector store 实例验证 where 过滤）

### TD-002: analysis_insight 召回无 session 边界与时间衰减，旧结论污染新分析
- **发现日期**: 2026-05-04
- **严重程度**: P1
- **影响范围**: core/memory — `search_by_type` 的 `analysis_insight` 通道 + `injection.py` 注入格式化
- **现象**: 新 session 执行相同供应商的价格差异分析时，报告中出现旧 session 保存的错误结论（"部分订单行存在物料信息缺失"），即使实际数据完全干净
- **根因**: `search_by_type` line 809 对 `analysis_insight` 走纯语义召回（`_search_by_type_hybrid`），不带 `session_id` 过滤或 `created_at` 时间衰减。旧 session 的 insight 被语义匹配后注入为 `[历史趋势参考]`，LLM 无法区分历史推测与当前事实
- **代码位置**: `core/memory/long_term.py:809`, `core/memory/injection.py:54-58`
- **修复状态**: 已修复（当前分支未提交）— 增加 `insight_recall_max_days=14` 时效窗口 + 注入层标注天数并提示"以本次数据为准"
- **测试缺口分析**:
  - 为什么现有测试未覆盖: 记忆注入的单元测试仅验证格式化输出正确性，未验证注入内容的时效性；无端到端测试模拟"session A 写入 insight → session B 不应召回过时 insight"的跨会话场景
  - 建议补充的测试场景: 1) 写入带 `created_at` 超过阈值的 `analysis_insight`，验证召回时被降权或过滤；2) 在注入层验证过旧 insight 被标注为"历史参考，需验证"而非直接呈现
  - 测试类型: 集成测试（跨 session 记忆生命周期）+ 单元测试（注入格式化的时效性标注逻辑）
