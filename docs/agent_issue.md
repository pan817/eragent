# 技术债清单

> 本文件统一记录项目所有技术债：架构异味、临时方案、性能隐患、待重构项、已知缺陷。
>
> **维护规则**：
> - 新增条目按发现日期倒序追加在"未修复"区。
> - 每条须包含：问题、影响、涉及文件、建议修复方向、发现日期。
> - **双记录约定**：每条技术债除了在本清单登记，还必须在涉及文件的关键位置写 `# TECH-DEBT(#N): <一句话>` 注释（`#N` 为本清单条目编号），便于在代码里快速定位。代码注释只承担"标记位置"职责，问题详情、影响、修复方案以本清单为准。
> - 修复后两处同步移除（代码注释删掉、清单条目移到"已修复"区，保留记录便于审计），并在 commit message 引用本文件条目编号。

---

## 未修复

### 1. 意图路由命中率优化
- **问题**：当前 IntentRouter 把较多查询兜底到 ReAct 路径（L3 LLM 分类），DAG 命中率不够高。
- **影响**：ReAct 路径延迟更高、token 消耗更多；DAG 模板的并行优势没充分利用。
- **建议修复**：扩充 `config/intent_seeds.yaml` 的语义种子覆盖度；调优 L2 Chroma 相似度阈值；把高频 ReAct 查询沉淀回 DAG 模板。
- **发现日期**：（早于 2026-04）

### 2. core / modules 反向依赖 api.schemas（分层违规）
- **问题**：业务层（`modules/p2p/*`）和编排层（`core/orchestrator/*`、`core/tasks/*`）反向 import `api.schemas.analysis` 里定义的 Pydantic 模型（`AnalysisRequest` / `AnalysisResult` / `Severity` / `ErrorInfo` 等）。违反"下层不依赖上层"的分层原则。
- **影响**：
  - core / modules 无法脱离 api 层独立测试和复用；
  - 未来若要把分析能力以 SDK 形式打包给非 HTTP 场景使用，会被 api 层的 FastAPI 依赖污染；
  - 暂不阻塞功能，FastAPI 项目里这种写法常见。
- **涉及文件**（共 11 个，6 个 core + 5 个 modules）：
  - [core/orchestrator/orchestrator.py:21](../core/orchestrator/orchestrator.py#L21)
  - [core/orchestrator/router.py](../core/orchestrator/router.py)
  - [core/orchestrator/intent.py](../core/orchestrator/intent.py)
  - [core/orchestrator/dag/templates.py](../core/orchestrator/dag/templates.py)
  - [core/tasks/registry.py](../core/tasks/registry.py)
  - [core/tasks/schemas.py](../core/tasks/schemas.py)
  - [modules/p2p/agent.py:21](../modules/p2p/agent.py#L21)
  - [modules/p2p/rules/three_way_match.py:14](../modules/p2p/rules/three_way_match.py#L14)
  - [modules/p2p/rules/payment_compliance.py:16](../modules/p2p/rules/payment_compliance.py#L16)
  - [modules/p2p/rules/price_variance.py:13](../modules/p2p/rules/price_variance.py#L13)
  - [modules/p2p/rules/supplier_performance.py:18](../modules/p2p/rules/supplier_performance.py#L18)
- **建议修复**：把 `api/schemas/analysis.py` 下沉到 `core/schemas/analysis.py`，作为业务数据契约的归属。`api/schemas/` 仅保留与 HTTP 协议强相关的 wrapper（如分页 envelope、错误响应格式），并 re-export `core.schemas` 中的核心模型，对外 API 兼容性不变。
- **发现日期**：2026-04-16（pyreverse 扫描 [packages_toplevel.png](architecture/packages_toplevel.png) 时定位）

---

## 已修复

（暂无）
