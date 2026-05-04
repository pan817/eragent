# ERP Analysis Agent (eragent)

## 项目概述
基于 LangChain 1.2.0 + OWL 本体论的 ERP 采购分析智能体。**当前为正式生产版本**（非 MVP），首发聚焦 P2P（Procure-to-Pay）模块，覆盖三路匹配、价格差异、付款合规、供应商绩效四大分析场景。支持多数据源（Oracle EBS / 新 ERP）通过 Protocol 抽象切换。

## 版本定位（生产标准）
- **正式生产版本**：所有改动必须以生产标准评估，不接受"先粗放再优化"的 MVP 心态。
- 工程要求：
  - 配置项要齐全、有合理生产默认值；关键行为可观测、可关闭、可调参。
  - 失败必须有日志/metric，不接受 `except: pass` 静默吞异常。任何降级必须有 WARNING 以上日志。
  - 关键路径(写入、检索、淘汰、迁移)必须有单元 + 集成测试覆盖。
  - 不引入"临时方案"或"待重构标记"，要么做完，要么不做。
  - 数据库变更走 alembic 迁移，不依赖 `create_all` 兜底。
  - 不写"调试用"分支或注释掉的代码。
  - **技术债登记（双记录）**：代码处写 `# TECH-DEBT(#N): <说明>`，同时在 [docs/issues/agent_issue.md](docs/issues/agent_issue.md) 追加完整条目。修复后两处同步移除，commit message 引用条目编号。
  - **测试缺陷登记**：所有在生产/手动测试中发现但自动化测试未覆盖的 bug，必须登记到 [docs/issues/test_defects.md](docs/issues/test_defects.md)，包含根因、代码位置、测试缺口分析（为什么测试没覆盖 + 建议补充场景）。审视完毕后迁移至 [docs/issues/test_defects_reviewed.md](docs/issues/test_defects_reviewed.md)。使用 `/review-defects` 触发例行审视。
  - 修改依赖时必须同步更新 `pyproject.toml` 和 `requirements.txt`，两者的依赖列表必须保持一致。
  - `alembic.ini` 禁止非 ASCII 字符（含中文注释），注释一律使用英文。
  - **测试通过要求**：小修改（bug fix、配置调整等）必须确保所有单元测试通过；新增/修改/重构大功能必须确保单元测试、端到端测试、API 测试等全部测试用例通过。
  - **API 输入校验**：所有外部输入（API 请求参数、用户查询文本）必须经 Pydantic 模型校验，禁止直接拼接到 SQL / Prompt 中，防止注入攻击。
  - **敏感信息脱敏**：日志中禁止打印 API Key、密码、用户原始查询中的敏感业务数据，必要时做掩码处理。
  - **LLM 调用超时与重试**：所有 LLM 调用必须设置超时，重试策略需有退避间隔和最大次数上限，避免雪崩。
  - **资源上限**：并发分析任务数、单次查询返回数据量、长期记忆存储条数等必须有硬上限，防止资源耗尽。
  - **优雅停机**：FastAPI shutdown 时必须等待进行中的分析任务完成或超时取消，不丢弃正在处理的请求。
  - **类型标注**：所有公开函数签名必须有完整类型标注（参数 + 返回值），内部函数建议标注。
  - **commit 规范**：commit message 遵循 Conventional Commits 格式（`feat/fix/refactor/test/docs(scope): 描述`）。
  - **关键指标埋点**：LLM 调用耗时、token 消耗、路由命中层级（L1/L2/L3）、DAG 任务成功/失败率等关键指标需可统计。
  - **查询安全**：禁止在循环中执行 SQL 查询（N+1 问题），批量操作使用 bulk insert/update，关注 ORM 生成 SQL 的性能。
  - **LangChain Tool 定义规范**：
    - 命名：动词 + 对象，动词区分语义（query=精确查/analyze=分析/detect=检测/find=发现/trace=追踪/compare=对比/calculate=计算/run=执行规则）。
    - docstring 必须包含四要素：①一句话说明（做什么）②适用场景（何时用）③不适用场景+推荐替代工具（何时不用）④返回内容（返回什么，可用于什么后续分析）。
    - 参数描述（Args）必须包含：业务含义、取值范围（枚举值或边界）、默认行为（空值/零值含义）、参数间组合关系（如有）。
    - 工具集注入必须精准：只注入当前模式下可用的工具，不注入返回降级信息或空结果的占位工具。
    - 新增工具时必须同步更新 system prompt 中的工具分组速查表（`modules/p2p/prompts.py`）。

## 技术栈
| 组件 | 选型 |
|------|------|
| Agent 框架 | LangChain 1.2.0（`create_agent` + 装饰器中间件） |
| LLM | Qwen（阿里云 Dashscope，ChatOpenAI 兼容接口，默认 qwen3-max；可切换 zhipu/openai/deepseek） |
| 本体推理 | Owlready2（OWL2 + SWRL 规则） |
| 时序知识图谱 | Graphiti（graphiti-core by Zep AI） |
| 图数据库 | Neo4j（默认启用，Graphiti ETL 同步 EBS 数据） |
| 向量数据库 | Chroma |
| 关系数据库 | PostgreSQL（EBS 镜像表 + 长期记忆 + 报告 + trace 存储） |
| ORM | SQLAlchemy（统一 engine，多模块共用） |
| Web 框架 | FastAPI |
| 配置管理 | config.yaml + Pydantic Settings |
| 测试 | pytest（覆盖率 ≥ 90%） |
| Python | ≥ 3.11 |

## 项目结构
```
eragent/
├── api/                         # REST API 层
│   ├── main.py                  # FastAPI 入口（lifespan 装配 EventBus + TaskRegistry）
│   ├── routes/                  # analyze / analyze_async / sessions / traces / etl / admin_metrics
│   └── schemas/                 # analysis / domain / session / trace
├── config/                      # config.yaml + crypto.py + settings.py
├── core/                        # 核心基础设施
│   ├── database/                # 统一 SQLAlchemy engine / session / Repository / init_db
│   ├── llm/model_factory.py     # 通用 LLM 创建（多 provider 切换）
│   ├── memory/                  # 长/短期记忆 + session recap + chat indexer + recency decay
│   ├── observability/           # tracing / streaming / store / checkpointer / display_labels
│   ├── etl/                     # Graphiti ETL（EBS → Neo4j 时序图谱）
│   │   ├── client / config / pipeline / scheduler / state / query_backend
│   │   ├── extractors/          # 5 域：master_data / purchasing / receiving / payables / sourcing
│   │   ├── transformers/        # registry（声明式映射）/ structured / llm_extractor
│   │   └── loaders/             # graphiti_loader（episode API 写入）
│   ├── ontology/                # OWL 加载与推理（loader / reasoner + SWRL）
│   ├── knowledge/               # embeddings（多 provider）/ Neo4j graph / Chroma vector_store
│   ├── orchestrator/            # 编排层
│   │   ├── orchestrator.py      # 同步/异步共用编排入口
│   │   ├── entity / lookup / param_extractor / unified_router / planner / prompts / provider / signal
│   │   ├── router/              # IntentRouter 三级路由（bypass→L1→L2→L3）
│   │   └── dag/                 # executor / templates / registry / validator / case_store / tables
│   ├── chat/                    # 会话历史持久化（独立于 LangGraph checkpointer）
│   ├── tasks/                   # 异步任务（registry / events memory&redis / schemas / context / stream_utils）
│   ├── time_utils.py            # 业务时区时间戳（now_cn / configure_timezone）
│   └── logging_utils.py
├── modules/p2p/                 # P2P 业务模块
│   ├── provider.py              # P2PModuleProvider（实现 ModuleProvider Protocol）
│   ├── intent_rules.py          # L1/L3 路由规则
│   ├── dag_templates.py         # P2P 专属 DAG 模板
│   ├── settings.py / repository.py / errors.py / agent.py / report_agent.py / prompts.py
│   ├── schemas/                 # 多数据源抽象
│   │   ├── protocol.py          # P2PRepositoryProtocol（跨数据源契约）
│   │   ├── oracle_ebs/          # Oracle EBS 实现（models / repository / graph_schema）
│   │   └── new_erp/             # 新 ERP 实现（models / repository / graph_schema）
│   ├── rules/                   # three_way_match / price_variance / payment_compliance / supplier_performance
│   ├── tools/                   # LangChain @tool 工具包（28 个）
│   │   ├── pg/                  # PostgreSQL 查询工具（query / analysis / advanced，15 个）
│   │   ├── graph/               # 图查询工具（search / entity / traversal / anomaly / comparison，12 个）
│   │   ├── chat_history.py      # 会话历史检索工具（1 个）
│   │   └── _inject.py / _output.py
│   ├── ontology/p2p.owl         # OWL 本体（非 Python 包）
│   └── mock_data/generator.py
├── docs/                        # 架构图、设计规格、问题追踪、测试、参考资料
├── migrations/                  # Alembic（已落地 15 个版本：0001 baseline → 0015 event_dates_to_timestamp）
├── scripts/                     # 部署辅助（deploy_timezone / backfill_chat_index / start.sh）
├── tests/                       # 67 单元 + 8 集成测试文件，pytest 收集 1663 用例
│   ├── conftest.py              # 基础 fixture（最小化）
│   ├── fixtures/p2p.py          # P2P 专属 fixture（pytest_plugins 引用，需 __init__.py）
│   ├── unit/ integration/ http/
├── alembic.ini / pyproject.toml / requirements.txt / .env(.example)
```

## Import 路径约定
所有模块使用**不带 `eragent.` 前缀**的绝对导入，项目根目录（`eragent/`）在 `sys.path` 中：
```python
from config.settings import Settings, get_settings
from core.database.engine import get_session
from modules.p2p.rules.three_way_match import ThreeWayMatchChecker
```

## 关键设计决策
- **统一数据库层**：长期记忆、可观测性 trace、分析报告共用 `core/database` 的 SQLAlchemy engine 与 session，各业务模块在自己的 `tables.py` 中声明表。
- **可观测性**：通过 LangChain 中间件采集 agent / tool 执行 trace，写入 PostgreSQL，可经 `/traces` API 查询。
- **ModuleProvider 解耦**：Orchestrator 通过 `ModuleProvider` Protocol 与业务模块交互（路由规则、DAG 模板、工具集、实体模式、记忆构建、Plan 提示词），不直接 import 模块代码。新增模块只需实现 Protocol 并在启动时注册。
- **多数据源支持**：`modules/p2p/schemas/protocol.py` 定义 `P2PRepositoryProtocol`，工具/规则只依赖该抽象；`oracle_ebs/` 与 `new_erp/` 各自实现 models + repository + graph_schema，按配置切换数据源。
- **记忆模块**：长期记忆按 `user_id` 隔离（PostgreSQL），短期记忆由 LangGraph PostgresSaver checkpointer 承担（按 `session_id` 隔离）。Session recap + chat history indexer 支持跨会话回忆。
- **编排粒度（DAG + Plan and Solve + ReAct 共存）**：L1/L2 命中 → DAG 并行执行 + ReportAgent 汇总；L3 兜底 → Planner（`core/orchestrator/planner.py`）一次 LLM 调用生成完整 DAG 计划，由 DAGExecutor 并行执行，替代 ReAct 多轮串行；规划失败/低置信度时降级到 ReAct 自主调用工具；早退路由（META/CHITCHAT/OUT_OF_SCOPE）→ 模板响应。意图模糊时不前置拦截，遵循"尽量回复"原则。
- **异步分析（SSE）**：`POST /analyze/async` + SSE 流式事件。`EventBus` 支持 memory（单进程）/ Redis（多 worker）双后端，多 worker 部署时 memory 后端 fail-fast。
- **会话历史**：`core/chat/` 独立持久化用户消息/助手回复（与 LangGraph checkpointer 短期记忆解耦），通过 `/sessions/*` API 暴露。
- **本体与规则分工**：合规规则（三路匹配、付款条款）用 SWRL 定义于本体，KPI 计算用 Python；上下文注入混合结构化 JSON + 自然语言。当前版本纯分析只读，写操作接口预留。
- **模型工厂**：`core/llm/model_factory.py` 提供通用 LLM 创建逻辑，便于切换模型 / 测试 mock。
- **时区约定**：全链路统一业务时区（默认 `Asia/Shanghai`），由 `app.timezone` / `APP_TIMEZONE` 配置。Python 侧**禁止** `datetime.utcnow()` / `datetime.now(timezone.utc)`，必须经 `core.time_utils.now_cn()` 产生时间戳。PG 引擎通过 `connect_args.options` 注入 `-c TimeZone=<tz>`。
- **Graphiti ETL**：`core/etl/` 将 PostgreSQL（EBS 镜像表）同步到 Graphiti（Neo4j）时序知识图谱。全量初始化 + 每 10 分钟增量同步（`LAST_UPDATE_DATE` 水位线）。5 域按依赖顺序：主数据 → 采购 → 收货 → 应付 → 寻源合同。20 张表声明式映射为 15 节点 + 19 边类型。
- **双后端查询模式**：`graphiti_etl.query_backend` 控制查询路径 — `graphiti` / `postgresql` / `hybrid`（图优先 SQL 降级）。通过 `QueryBackend` Protocol 透明切换，过渡期使用 `hybrid`。
- **工具集**：PG 工具 15 个 + 图工具 12 个 + chat 工具 1 个 = 28 个 LangChain `@tool`，按 `pg/` 与 `graph/` 分包组织，统一 `_inject.py` 注入 Repository / GraphitiClient / QueryBackend。

## 配置要点
- 敏感信息通过环境变量注入：`LLM_API_KEY`、`LLM_FAST_API_KEY`、`NEO4J_PASSWORD`、`POSTGRES_PASSWORD`
- 双模型架构：`llm`（主模型，ReAct + tool-calling）+ `llm_fast`（轻量任务：报告生成、L3 意图分类、Planner）。`llm_fast` 未配置的字段自动从 `llm` 镜像；不硬编码模型名，切换只改配置不改代码。
- Neo4j 代码层默认启用（`Neo4jSettings.enabled=True`），但 `config.yaml` 中覆盖为 `false`，用户按需开启。
- Graphiti ETL 配置在 `graphiti_etl` 段，支持 `ETL_*` 环境变量覆盖。
- Embedding provider 可选：default / fake / openai / dashscope / zhipu。
- 数据源切换：通过配置选择 `oracle_ebs` 或 `new_erp`，对应 `modules/p2p/schemas/` 下的实现。

## 运行测试
```bash
cd eragent
pip install -e ".[dev]"
pytest --cov=. --cov-report=term-missing --cov-fail-under=90
```

## 本地服务
- 部署端口：**8080**（`http://localhost:8080`）
- 初始化数据：`POST /api/v1/ptp-agent/init-data?sync_to_neo4j=true`

## 数据库迁移
```bash
alembic upgrade head                              # 升到最新版本
alembic current                                   # 查看当前版本
alembic revision --autogenerate -m "描述"          # 生成新迁移
```

## __init__.py 约定
- 仅在 setuptools 需要识别的 Python 包目录中保留 `__init__.py`
- `tests/` 目录及子目录不需要 `__init__.py`（pytest 自动发现，`tests/fixtures/` 除外，需要供 `pytest_plugins` 引用）
- `modules/p2p/ontology/` 仅存放 OWL 文件，不是 Python 包，无 `__init__.py`
- `modules/p2p/rules/__init__.py` 提供四个规则类的统一导出
- `modules/p2p/tools/__init__.py` 提供全部 28 个 @tool 函数的统一导出（PG 15 + Graph 12 + Chat 1）
- `core/orchestrator/router/__init__.py` 承载 IntentRouter 三级路由核心逻辑

## 当前进度
- 所有功能模块及七阶段架构重构（Phase 1 → 7.3）已全部完成
- Graphiti ETL 全部 6 个 Phase（0-6）已完成：表结构改造、ETL 基础设施、Pipeline/Scheduler、12 个图查询工具、双后端查询模式
- 多数据源改造已落地：`P2PRepositoryProtocol` 抽象 + `oracle_ebs`/`new_erp` 双实现
- Plan and Solve 已接管 lookup 关键词推断与 L3 兜底路径
- 长程对话记忆 v2：session recap + chat indexer + recency decay + idle watcher
- Alembic 迁移体系已落地 15 个版本（0001 baseline → 0015 event_dates_to_timestamp）
- 单元测试 67 个文件 + 集成测试 8 个文件（pytest 收集 1663 用例，覆盖率 90.36%）
- 端到端测试（真实 LLM）通过
- 架构层技术债见 [docs/issues/agent_issue.md](docs/issues/agent_issue.md)
