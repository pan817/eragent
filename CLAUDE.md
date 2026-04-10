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

## 技术栈
| 组件 | 选型 |
|------|------|
| Agent 框架 | LangChain 1.2.0（`create_agent` + 装饰器中间件） |
| LLM | GLM-4（智谱，ChatOpenAI 兼容接口，配置化可切换） |
| 本体推理 | Owlready2（OWL2 + SWRL 规则） |
| 图数据库 | Neo4j |
| 向量数据库 | Chroma |
| 关系数据库 | PostgreSQL（长期记忆 + 报告 + 可观测性 trace 存储） |
| ORM | SQLAlchemy（统一 engine，多模块共用） |
| Web 框架 | FastAPI |
| 配置管理 | config.yaml + Pydantic Settings |
| 测试 | pytest（覆盖率 ≥ 85%） |
| Python | ≥ 3.11 |

## 项目结构
```
eragent/
├── api/                         # REST API 层
│   ├── main.py                  # FastAPI 应用入口
│   ├── routes/
│   │   ├── analyze.py           # 分析路由
│   │   └── traces.py            # 可观测性 trace 查询路由
│   └── schemas/
│       ├── analysis.py          # 分析请求/响应模型
│       └── trace.py             # trace 响应模型
├── config/
│   ├── config.yaml              # 结构化配置文件
│   └── settings.py              # Pydantic Settings 配置管理
├── core/                        # 核心基础设施
│   ├── database/                # 统一数据库层（SQLAlchemy）
│   │   ├── engine.py            # engine / session 工厂
│   │   ├── models.py            # Declarative Base
│   │   ├── repository.py        # 通用 Repository
│   │   └── init_db.py           # 建表入口
│   ├── memory/                  # 短期/长期记忆（拆包）
│   │   ├── short_term.py        # 会话内短期记忆
│   │   ├── long_term.py         # 跨会话长期记忆（按 user_id 隔离）
│   │   └── tables.py            # ORM 表定义
│   ├── observability/           # 可观测性
│   │   ├── middleware.py        # LangChain 中间件，采集 agent/tool 调用
│   │   ├── store.py             # trace 持久化
│   │   ├── console.py           # 控制台输出
│   │   └── tables.py            # trace ORM 表
│   ├── ontology/                # OWL 本体加载和推理
│   │   ├── loader.py
│   │   └── reasoner.py          # 推理器 + SWRL 规则
│   ├── knowledge/
│   │   ├── graph.py             # Neo4j 封装
│   │   └── vector_store.py      # Chroma 封装
│   ├── orchestrator/
│   │   ├── intent.py            # 意图解析
│   │   └── orchestrator.py      # 分析任务编排器
│   └── logging_utils.py         # 轻量日志工具
├── modules/p2p/                 # P2P 业务模块
│   ├── rules/                   # 业务规则引擎（__init__.py 提供统一导出）
│   │   ├── three_way_match.py
│   │   ├── price_variance.py
│   │   ├── payment_compliance.py
│   │   ├── supplier_performance.py
│   │   └── _utils.py            # 规则共享工具
│   ├── ontology/p2p.owl         # P2P 领域 OWL 本体（非 Python 包）
│   ├── tools.py                 # LangChain @tool 工具集
│   ├── prompts.py               # Agent 提示词模板
│   ├── model_factory.py         # LLM 客户端工厂（配置化切换模型）
│   ├── agent.py                 # P2P Agent（create_agent）
│   └── mock_data/generator.py   # 模拟数据生成器
├── docs/
│   ├── erp_agent_spec.md        # 系统设计规格
│   ├── erp_procurement_agent.pdf
│   ├── thought.md
│   └── execute.md
├── tests/                       # 测试（无 __init__.py）
│   ├── conftest.py
│   ├── unit/                    # 单元测试
│   ├── integration/             # 集成测试（含 test_e2e.py）
│   └── http/                    # .http 调试用例
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
from core.observability.middleware import ObservabilityMiddleware
from modules.p2p.rules.three_way_match import ThreeWayMatchChecker
```

## 关键设计决策
- **统一数据库层**：长期记忆、可观测性 trace、分析报告共用 `core/database` 的 SQLAlchemy engine 与 session，各业务模块在自己的 `tables.py` 中声明表。
- **可观测性**：通过 LangChain 中间件采集 agent / tool 执行 trace，写入 PostgreSQL，可经 `/traces` API 查询。
- **记忆模块拆包**：原 `core/memory.py` 拆为 `core/memory/` 包，区分 `short_term` / `long_term` / `tables`。
- **编排粒度**：当前采用粗粒度——Orchestrator 路由到 P2P Agent，Agent 内部串行处理。预留接口支持细粒度 DAG 调度。
- **本体上下文注入**：混合模式——关键规则用结构化 JSON，业务背景用自然语言。
- **SWRL 规则 vs Python 代码**：合规规则（三路匹配、付款条款）用 SWRL 定义于本体；KPI 计算用 Python 实现。
- **纯分析只读**：当前版本不执行 ERP 写操作，写操作接口预留。
- **记忆隔离**：长期记忆按 `user_id` 隔离，短期记忆按 `session_id` 隔离。
- **模型工厂**：`modules/p2p/model_factory.py` 集中创建 LLM 客户端，便于切换模型 / 测试 mock。

## 配置要点
- 敏感信息通过环境变量注入：`LLM_API_KEY`、`NEO4J_PASSWORD`、`POSTGRES_PASSWORD`
- 三路匹配容差支持按供应商/物料类别/金额区间配置
- 默认分析时间范围 30 天，可配置
- 异常严重等级：超容差 2 倍以上或金额 > 50 万为 HIGH

## 运行测试
```bash
cd eragent
pip install -e ".[dev]"
pytest --cov=. --cov-report=term-missing --cov-fail-under=85
```

## __init__.py 约定
- 仅在 setuptools 需要识别的 Python 包目录中保留 `__init__.py`
- `tests/` 目录及其子目录不需要 `__init__.py`（pytest 自动发现）
- `modules/p2p/ontology/` 仅存放 OWL 文件，不是 Python 包，无 `__init__.py`
- `modules/p2p/rules/__init__.py` 提供四个规则类的统一导出

## 当前进度
- 所有功能模块代码已完成，新增统一数据库层 + 可观测性 + 拆包后的 memory 模块
- 单元测试 + 集成测试覆盖率 ≥ 95%
- 端到端测试（真实 LLM）通过
