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
  - **技术债登记（双记录）**：代码处写 `# TECH-DEBT(#N): <说明>`，同时在 [docs/agent_issue.md](docs/agent_issue.md) 追加完整条目。修复后两处同步移除，commit message 引用条目编号。
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
│       └── analysis / domain / session / trace
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
│   ├── llm/                     # LLM 客户端工厂
│   │   └── model_factory.py     # 通用 LLM 创建逻辑（从 modules/p2p 提升）
│   ├── memory/                  # 记忆管理
│   │   ├── long_term.py         # 跨会话长期记忆（按 user_id 隔离，PostgreSQL）
│   │   ├── short_term.py        # 短期记忆辅助
│   │   ├── trimmer.py           # 记忆裁剪器
│   │   └── tables.py            # ORM 表定义
│   ├── observability/           # 可观测性
│   │   ├── checkpointer.py      # LangGraph PostgresSaver trace 补丁（幂等挂载）
│   │   ├── tracing.py           # 链路追踪核心
│   │   ├── streaming.py         # 流式输出追踪
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
│   │   ├── entity.py            # 实体抽取 + 指代消解
│   │   ├── prompts.py           # 编排层提示词模板
│   │   ├── provider.py          # ModuleProvider Protocol（业务模块能力接口）
│   │   ├── router/              # IntentRouter 三级路由包（bypass→L1→L2→L3）
│   │   │   └── __init__.py      # 路由核心逻辑
│   │   ├── intent.py            # 意图分类辅助 / IntentKind 兼容旧入口
│   │   ├── signal.py            # QuerySignal / IntentKind 数据契约
│   │   └── dag/                 # DAG 执行子模块
│   │       └── executor / templates / registry / validator / case_store / tables
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
│   ├── provider.py              # P2PModuleProvider（实现 ModuleProvider Protocol）
│   ├── intent_rules.py          # L1/L3 路由规则（关键词 + 分析类型描述）
│   ├── dag_templates.py         # P2P 专属 DAG 模板（从 core 迁入）
│   ├── settings.py              # P2PSettings 模块级配置
│   ├── repository.py            # P2PRepository（数据查询）
│   ├── rules/                   # 业务规则引擎（__init__.py 提供统一导出）
│   │   └── three_way_match / price_variance / payment_compliance / supplier_performance / _utils
│   ├── tools/                   # LangChain @tool 工具包（19 个工具）
│   │   └── query / analysis / advanced / stub / _inject / _output
│   ├── ontology/p2p.owl         # P2P 领域 OWL 本体（非 Python 包）
│   ├── prompts.py               # Agent 提示词模板
│   ├── model_factory.py         # LLM 客户端工厂（qwen / zhipu / minimax / deepseek / openai）
│   ├── agent.py                 # P2P Agent（create_agent）
│   ├── report_agent.py          # ReportAgent（DAG 路径 LLM 报告生成）
│   ├── errors.py                # 业务异常类型
│   └── mock_data/generator.py   # 模拟数据生成器
├── docs/                        # 文档（技术债清单 agent_issue.md、架构图、设计规格等）
├── migrations/                  # Alembic 数据库迁移
│   ├── env.py
│   ├── script.py.mako
│   └── versions/                # 已落地 9 个版本（0001 baseline → 0009 reports_trace_id）
├── tests/                       # 测试（无 __init__.py）
│   ├── conftest.py              # 基础 fixture（最小化，P2P fixture 已拆出）
│   ├── fixtures/                # 按模块拆分的测试 fixture
│   │   └── p2p.py               # P2P 专属 fixture（p2p_settings, repository, mock_po_data）
│   ├── unit/                    # 单元测试（36 个文件）
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
from core.database.engine import get_session
from modules.p2p.rules.three_way_match import ThreeWayMatchChecker
```

## 关键设计决策
- **统一数据库层**：长期记忆、可观测性 trace、分析报告共用 `core/database` 的 SQLAlchemy engine 与 session，各业务模块在自己的 `tables.py` 中声明表。
- **可观测性**：通过 LangChain 中间件采集 agent / tool 执行 trace，写入 PostgreSQL，可经 `/traces` API 查询。
- **ModuleProvider 解耦**：Orchestrator 通过 `ModuleProvider` Protocol 与业务模块交互（路由规则、DAG 模板、工具集、实体模式、记忆构建），不直接 import 模块代码。新增模块只需实现 Protocol 并在启动时注册。
- **记忆模块**：长期记忆按 `user_id` 隔离（PostgreSQL），短期记忆由 LangGraph PostgresSaver checkpointer 承担（按 `session_id` 隔离）。
- **编排粒度（DAG + ReAct 共存）**：L1/L2 命中 → DAG 并行执行 + ReportAgent 汇总；L3/低置信度 → ReAct 自主调用工具；早退路由（META/CHITCHAT/CLARIFICATION/OUT_OF_SCOPE）→ 直接模板响应。
- **异步分析（SSE）**：`POST /analyze/async` + SSE 流式事件。`EventBus` 支持 memory（单进程）/ Redis（多 worker）双后端，多 worker 部署时 memory 后端 fail-fast。
- **会话历史**：`core/chat/` 独立持久化用户消息/助手回复（与 LangGraph checkpointer 短期记忆解耦），通过 `/sessions/*` API 暴露。
- **本体与规则分工**：合规规则（三路匹配、付款条款）用 SWRL 定义于本体，KPI 计算用 Python；上下文注入混合结构化 JSON + 自然语言。当前版本纯分析只读，写操作接口预留。
- **模型工厂**：`core/llm/model_factory.py` 提供通用 LLM 创建逻辑，`modules/p2p/model_factory.py` 封装 P2P 特定配置，便于切换模型 / 测试 mock。
- **时区约定**：全链路统一业务时区（默认 `Asia/Shanghai`），由 `app.timezone` / `APP_TIMEZONE` 配置。
  - Python 侧**禁止** `datetime.utcnow()` / `datetime.now(timezone.utc)`，必须经 `core.time_utils.now_cn()` 产生时间戳。PG 引擎通过 `connect_args.options` 注入 `-c TimeZone=<tz>`。

## 配置要点
- 敏感信息通过环境变量注入：`LLM_API_KEY`、`LLM_FAST_API_KEY`、`NEO4J_PASSWORD`、`POSTGRES_PASSWORD`
- 双模型架构：`llm`（主模型，ReAct + tool-calling）+ `llm_fast`（轻量任务：报告生成、L3 意图分类）。`llm_fast` 未配置的字段自动从 `llm` 镜像
  - 不硬编码模型名，切换只改配置不改代码
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

## __init__.py 约定
- 仅在 setuptools 需要识别的 Python 包目录中保留 `__init__.py`
- `tests/` 目录及其子目录不需要 `__init__.py`（pytest 自动发现，`tests/fixtures/` 除外需要 `__init__.py` 供 `pytest_plugins` 引用）
- `modules/p2p/ontology/` 仅存放 OWL 文件，不是 Python 包，无 `__init__.py`
- `modules/p2p/rules/__init__.py` 提供四个规则类的统一导出
- `modules/p2p/tools/__init__.py` 提供全部 19 个 @tool 函数的统一导出
- `core/orchestrator/router/__init__.py` 承载 IntentRouter 三级路由核心逻辑

## 当前进度
- 所有功能模块及七阶段架构重构（Phase 1 → 7.3）已全部完成
- Alembic 迁移体系已落地 9 个版本
- 单元测试 36 个文件 + 集成测试 6 个文件，覆盖率 ≥ 90%（pytest 收集 1000+ 用例）
- 端到端测试（真实 LLM）通过
- 架构层技术债见 [docs/agent_issue.md](docs/agent_issue.md)
