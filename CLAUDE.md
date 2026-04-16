# ERP Analysis Agent (eragent)

## 项目概述
基于 LangChain 1.2.0 + OWL 本体论的 ERP 采购分析智能体。**当前为正式生产版本**（非 MVP），首发聚焦 P2P（Procure-to-Pay）模块，覆盖三路匹配、价格差异、付款合规、供应商绩效四大分析场景。

## 版本定位（生产标准）
- **正式生产版本**：所有改动必须以生产标准评估，不接受"先粗放再优化"的 MVP 心态。
- 工程要求：
  - 配置项要齐全、有合理生产默认值；关键行为可观测、可关闭、可调参。
  - 失败必须有日志/metric，不接受 `except: pass` 静默吞异常。任何降级必须有 WARNING 以上日志。
  - 关键路径(写入、检索、淘汰、迁移)必须有单元 + 集成测试覆盖。
  - 不引入"临时方案"或"待重构标记"，要么做完，要么不做。
  - 数据库变更走 alembic 迁移，不依赖 `create_all` 兜底。
  - 不写"调试用"分支或注释掉的代码。
  - **技术债登记（双记录）**：所有技术债（架构异味、临时方案、性能隐患、待重构项、已知缺陷）必须**同时**记录到两处：
    1. **代码注释**：在涉及文件的关键位置写 `# TECH-DEBT(#N): <一句话说明>`，`#N` 为 [docs/agent_issue.md](docs/agent_issue.md) 中的条目编号，便于本地快速定位。
    2. **汇总清单**：在 [docs/agent_issue.md](docs/agent_issue.md) 追加完整条目（问题/影响/涉及文件/建议修复/发现日期）。代码注释只承担"标记位置"职责，详细信息以汇总清单为准。
    修复后两处同步移除（代码注释删掉、清单条目移到"已修复"区），并在 commit message 引用条目编号。
  - 修改依赖时必须同步更新 `pyproject.toml` 和 `requirements.txt`，两者的依赖列表必须保持一致。
  - `alembic.ini` 禁止包含非 ASCII 字符（如中文注释）。原因：Alembic 和 `logging.config.fileConfig` 使用 `configparser` 读取 ini 文件时采用系统 locale 编码，Windows 中文系统为 GBK，无法解码 UTF-8 中文，导致启动失败。注释一律使用英文。

## 技术栈
| 组件 | 选型 |
|------|------|
| Agent 框架 | LangChain 1.2.0（`create_agent` + 装饰器中间件） |
| LLM | Qwen（阿里云 Dashscope，ChatOpenAI 兼容接口，默认 qwen3-max；可切换 zhipu/openai/deepseek） |
| 本体推理 | Owlready2（OWL2 + SWRL 规则） |
| 图数据库 | Neo4j |
| 向量数据库 | Chroma |
| 关系数据库 | PostgreSQL（长期记忆 + 报告 + 可观测性 trace 存储） |
| ORM | SQLAlchemy（统一 engine，多模块共用） |
| Web 框架 | FastAPI |
| 配置管理 | config.yaml + Pydantic Settings |
| 测试 | pytest（覆盖率 ≥ 90%） |
| Python | ≥ 3.11 |

## 项目结构
```
eragent/
├── api/                         # REST API 层
│   ├── main.py                  # FastAPI 应用入口（lifespan 装配 EventBus + TaskRegistry）
│   ├── routes/
│   │   ├── analyze.py           # 同步分析路由（POST /analyze）
│   │   ├── analyze_async.py     # 异步分析 + SSE 流式事件路由
│   │   ├── sessions.py          # 会话历史 CRUD 路由
│   │   └── traces.py            # 可观测性 trace 查询路由
│   └── schemas/
│       ├── analysis.py          # 分析请求/响应模型
│       ├── session.py           # 会话历史响应模型
│       └── trace.py             # trace 响应模型
├── config/
│   ├── config.yaml              # 结构化配置文件
│   ├── intent_seeds.yaml        # IntentRouter L2 语义检索种子集
│   ├── crypto.py                # 敏感配置字段加密/解密工具
│   └── settings.py              # Pydantic Settings 配置管理
├── core/                        # 核心基础设施
│   ├── database/                # 统一数据库层（SQLAlchemy）
│   │   ├── engine.py            # engine / session 工厂
│   │   ├── models.py            # Declarative Base
│   │   ├── repository.py        # 通用 Repository
│   │   └── init_db.py           # 建表入口
│   ├── memory/                  # 记忆管理
│   │   ├── long_term.py         # 跨会话长期记忆（按 user_id 隔离，PostgreSQL）
│   │   ├── middleware.py        # MemoryMiddleware：ReAct 循环内 LLM 输入裁剪
│   │   └── tables.py            # ORM 表定义
│   ├── observability/           # 可观测性
│   │   ├── checkpointer.py      # LangGraph PostgresSaver trace 补丁（幂等挂载）
│   │   ├── middleware.py        # LangChain 中间件，采集 agent/tool 调用
│   │   ├── store.py             # trace 持久化
│   │   ├── console.py           # 控制台输出
│   │   ├── display_labels.py    # span 类型 → 中文展示名映射
│   │   └── tables.py            # trace ORM 表
│   ├── ontology/                # OWL 本体加载和推理
│   │   ├── loader.py
│   │   └── reasoner.py          # 推理器 + SWRL 规则
│   ├── knowledge/
│   │   ├── embeddings.py        # Embedding Provider 抽象（default/openai/fake/dashscope/zhipu）
│   │   ├── graph.py             # Neo4j 封装
│   │   └── vector_store.py      # Chroma 封装
│   ├── orchestrator/            # 编排层
│   │   ├── orchestrator.py      # 分析任务编排器（同步/异步共用入口）
│   │   ├── router.py            # IntentRouter 三级路由（bypass→L1→L2→L3）
│   │   ├── intent.py            # 意图分类辅助 / IntentKind 兼容旧入口
│   │   ├── signal.py            # QuerySignal / IntentKind 数据契约
│   │   └── dag/                 # DAG 执行子模块
│   │       ├── executor.py      # DAGExecutor 并行任务调度
│   │       ├── templates.py     # 静态 DAG 模板（按 AnalysisType）
│   │       ├── registry.py      # 任务/工具映射注册
│   │       ├── validator.py     # DAG 拓扑校验
│   │       ├── case_store.py    # 自学习案例存储（PG + Chroma）
│   │       └── tables.py        # DAG 案例 ORM 表
│   ├── chat/                    # 会话历史持久化（独立于短期记忆 checkpointer）
│   │   ├── repository.py        # ChatRepository（消息读写 + 搜索）
│   │   └── tables.py            # 会话/消息 ORM 表
│   ├── tasks/                   # 异步任务基础设施（POST /analyze/async + SSE）
│   │   ├── registry.py          # TaskRegistry 后台 runner + 生命周期管理
│   │   ├── events.py            # MemoryEventBus（单进程）
│   │   ├── events_redis.py      # RedisEventBus（多 worker 必选）
│   │   ├── schemas.py           # AnalysisTaskAck / TaskStatus 响应模型
│   │   ├── context.py           # 任务上下文 + ContextVar
│   │   └── stream_utils.py      # SSE 帧打包工具
│   ├── time_utils.py            # 业务时区时间戳工具（now_cn / configure_timezone）
│   └── logging_utils.py         # 轻量日志工具
├── modules/p2p/                 # P2P 业务模块
│   ├── rules/                   # 业务规则引擎（__init__.py 提供统一导出）
│   │   ├── three_way_match.py
│   │   ├── price_variance.py
│   │   ├── payment_compliance.py
│   │   ├── supplier_performance.py
│   │   └── _utils.py            # 规则共享工具
│   ├── ontology/p2p.owl         # P2P 领域 OWL 本体（非 Python 包）
│   ├── tools.py                 # LangChain @tool 工具集（19 个）
│   ├── prompts.py               # Agent 提示词模板
│   ├── model_factory.py         # LLM 客户端工厂（qwen / zhipu / minimax / deepseek / openai）
│   ├── agent.py                 # P2P Agent（create_agent）
│   ├── report_agent.py          # ReportAgent（DAG 路径 LLM 报告生成）
│   ├── errors.py                # 业务异常类型
│   └── mock_data/generator.py   # 模拟数据生成器
├── docs/
│   ├── agent_issue.md                 # **技术债清单（统一登记入口）**
│   ├── architecture/                  # 架构图（pyreverse + 手绘 dot）
│   ├── erp_agent_spec.md              # 系统设计规格
│   ├── erp_procurement_agent.pdf
│   └── ...                            # 其它专题设计文档（async/long_term/sse/intent 等）
├── migrations/                  # Alembic 数据库迁移
│   ├── env.py
│   ├── script.py.mako
│   └── versions/                # 已落地 9 个版本：0001 baseline → 0009 reports_trace_id
│       ├── 0001_baseline.py
│       ├── 0002_memories_content_hash.py
│       ├── 0003_memories_rename_metadata_to_attrs.py
│       ├── 0004_reports_user_created_idx.py
│       ├── 0005_dag_cases.py
│       ├── 0006_chat_sessions.py
│       ├── 0007_trace_composite_indexes.py
│       ├── 0008_timezone_normalization.py
│       └── 0009_reports_trace_id.py
├── tests/                       # 测试（无 __init__.py）
│   ├── conftest.py
│   ├── unit/                    # 单元测试（35 个文件）
│   ├── integration/             # 集成测试（含 test_e2e.py / test_async_multi_worker.py）
│   └── http/                    # .http 调试用例
├── alembic.ini                  # Alembic 配置（script_location=migrations）
├── .env                         # 环境变量（不提交）
├── .env.example
└── pyproject.toml
```

## Import 路径约定
所有模块使用**不带 `eragent.` 前缀**的绝对导入，项目根目录（`eragent/`）在 `sys.path` 中：
```python
from config.settings import Settings, get_settings
from api.schemas.analysis import AnalysisResult, Severity
from core.database.engine import get_session
from core.memory.long_term import LongTermMemory
from core.observability.middleware import TimingMiddleware
from modules.p2p.rules.three_way_match import ThreeWayMatchChecker
```

## 关键设计决策
- **统一数据库层**：长期记忆、可观测性 trace、分析报告共用 `core/database` 的 SQLAlchemy engine 与 session，各业务模块在自己的 `tables.py` 中声明表。
- **可观测性**：通过 LangChain 中间件采集 agent / tool 执行 trace，写入 PostgreSQL，可经 `/traces` API 查询。
- **记忆模块拆包**：原 `core/memory.py` 拆为 `core/memory/` 包，仅保留 `long_term` / `tables`。短期记忆由 LangGraph PostgresSaver checkpointer 承担（以 `session_id` 作为 `thread_id`），通过 `core/observability/checkpointer.py` 注入 trace 监控，无需独立 `short_term.py`。
- **编排粒度（DAG + ReAct 共存）**：Orchestrator 同时支持两条执行路径——
  - L1/L2 命中（关键词或 Chroma 语义命中）→ 加载 `core/orchestrator/dag/templates.py` 静态 DAG → `DAGExecutor` 并行执行 → `ReportAgent` 汇总报告；
  - L3（LLM 兜底）/ 低置信度 / RECALL / DATA_LOOKUP / COMPREHENSIVE 无实体 → P2PAgent ReAct（LangGraph create_agent）自主串行调用工具；
  - 早退路由（META / CHITCHAT / CLARIFICATION / OUT_OF_SCOPE）→ 不进入 DAG / ReAct，直接由 Orchestrator 渲染模板响应。
- **异步分析（SSE）**：除 `POST /analyze` 同步入口外，提供 `POST /analyze/async` + `GET /analyze/tasks/{trace_id}/events` SSE 流式接口。`core/tasks/registry.py` 维护后台 runner，事件经 `EventBus`（memory 单进程 / Redis 多 worker，由 `async_analysis.event_backend` 配置）下发。多 worker 部署时 memory 后端会被启动期 `_check_event_backend_matches_workers` 直接 fail-fast。
- **会话历史**：`core/chat/` 独立持久化用户消息/助手回复（与 LangGraph checkpointer 短期记忆解耦），通过 `/sessions/*` API 暴露。
- **本体上下文注入**：混合模式——关键规则用结构化 JSON，业务背景用自然语言。
- **SWRL 规则 vs Python 代码**：合规规则（三路匹配、付款条款）用 SWRL 定义于本体；KPI 计算用 Python 实现。
- **纯分析只读**：当前版本不执行 ERP 写操作，写操作接口预留。
- **记忆隔离**：长期记忆按 `user_id` 隔离，短期记忆按 `session_id` 隔离。
- **模型工厂**：`modules/p2p/model_factory.py` 集中创建 LLM 客户端，便于切换模型 / 测试 mock。
- **时区约定**：全链路统一使用业务时区（默认 `Asia/Shanghai`），由 `app.timezone` / `APP_TIMEZONE` 配置。
  - Python 侧**禁止**直接调用 `datetime.utcnow()` 或 `datetime.now(timezone.utc)`，必须经 `core.time_utils.now_cn()` 产生时间戳；写表时也不要依赖 `server_default=func.now()` 作为唯一来源。
  - PostgreSQL 引擎（SQLAlchemy + LangGraph PostgresSaver）通过 `connect_args.options` / conninfo `?options=...` 注入 `-c TimeZone=<tz>`，让 `now()` / `CURRENT_TIMESTAMP` / TIMESTAMPTZ 展示值全部走业务时区。
  - trace 表 (`trace_runs` / `trace_spans`) 已升级为 `TIMESTAMPTZ`（迁移 `0008_timezone_normalization`）。
  - SQLite 测试环境由 `tests/conftest.py` 调用 `configure_timezone` + `install_sqlite_timezone_hook` 模拟北京时间。

## 配置要点
- 敏感信息通过环境变量注入：`LLM_API_KEY`、`LLM_FAST_API_KEY`、`NEO4J_PASSWORD`、`POSTGRES_PASSWORD`
- 双模型架构：
  - `llm`：主模型，承担 P2P Agent ReAct + tool-calling（默认 qwen3-max）
  - `llm_fast`：小模型，承担 ReportAgent 报告生成、IntentRouter L3 意图分类（轻量任务）
  - 字段级继承：`llm_fast` 任一字段未在 YAML / env 显式配置时，`Settings.from_yaml` 会在启动期自动从 `llm` 对应字段镜像（测试环境无需单独配置 llm_fast）
  - 代码层对两套配置一视同仁，不硬编码具体模型名；切换只改配置不改代码
- 三路匹配容差支持按供应商/物料类别/金额区间配置；默认 5%，最大 10%
- 默认分析时间范围 30 天，可配置（最大 365 天）
- 异常严重等级：超容差 2 倍以上或金额 > 50 万为 HIGH
- 长期记忆：最多检索 5 条，RRF 融合（k=60），每用户上限 200 条，去重窗口 900s，内容最短 50 字符
- Neo4j 默认禁用（`neo4j.enabled: false`），可按需开启
- Embedding provider 可选：default / fake / openai / dashscope / zhipu

## 运行测试
```bash
cd eragent
pip install -e ".[dev]"
pytest --cov=. --cov-report=term-missing --cov-fail-under=90
```

## 数据库迁移
```bash
# 执行迁移（升到最新版本）
alembic upgrade head

# 查看当前版本
alembic current

# 生成新迁移（修改模型后）
alembic revision --autogenerate -m "描述"
```
迁移文件位于 `migrations/versions/`，已应用 9 个版本（0001 baseline → 0009 reports_trace_id）。

## __init__.py 约定
- 仅在 setuptools 需要识别的 Python 包目录中保留 `__init__.py`
- `tests/` 目录及其子目录不需要 `__init__.py`（pytest 自动发现）
- `modules/p2p/ontology/` 仅存放 OWL 文件，不是 Python 包，无 `__init__.py`
- `modules/p2p/rules/__init__.py` 提供四个规则类的统一导出

## 当前进度
- 所有功能模块代码已完成，包含统一数据库层、可观测性、拆包后的 memory 模块
- **Orchestrator 三级路由 + DAG 执行层 + 自学习闭环**已完成（Phase 1/2/3）
  - 三级意图路由：L1 关键词命中率 → L2 Chroma 语义匹配 → L3 LLM 分类
  - DAG 并行执行器 + 静态模板（4 种分析类型）+ ReAct 兜底（共存模式）
  - 自学习案例存储（PostgreSQL 权威 + Chroma 缓存，服务启动时从 PG 加载）
  - 全链路监控：intent / dag / dag.task / tool / model / report / checkpoint / case_store span
- **异步分析 + SSE**：`POST /analyze/async` + `TaskRegistry` + `EventBus`（memory / Redis 双后端）+ `GET .../events` 流式事件已落地，跨 worker 部署支持。
- **会话历史 API**：`/sessions/*` CRUD + 消息搜索已落地，使用独立 `core/chat/` 模块持久化。
- Alembic 迁移体系建立，已落地 9 个版本（baseline → content_hash → rename_attrs → reports_idx → dag_cases → chat_sessions → trace_indexes → timezone → reports_trace_id）
- 单元测试 35 个文件 + 集成测试 6 个文件，覆盖率 ≥ 90%（pytest 收集 1000+ 用例）
- 端到端测试（真实 LLM）通过
- 近期优化重点：长期记忆检索质量（RRF 融合、去重、内容长度过滤）；架构层技术债见 [docs/agent_issue.md](docs/agent_issue.md)
