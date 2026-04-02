# ERP Analysis Agent (eragent)

## 项目概述
基于 LangChain 1.2.0 + OWL 本体论的 ERP 采购分析智能体。MVP 聚焦 P2P（Procure-to-Pay）模块，覆盖三路匹配、价格差异、付款合规、供应商绩效四大分析场景。

## 技术栈
| 组件 | 选型 |
|------|------|
| Agent 框架 | LangChain 1.2.0（`create_agent` + 装饰器中间件） |
| LLM | GLM-4（智谱，ChatOpenAI 兼容接口，配置化可切换） |
| 本体推理 | Owlready2（OWL2 + SWRL 规则） |
| 图数据库 | Neo4j |
| 向量数据库 | Chroma（MVP） |
| 关系数据库 | PostgreSQL（长期记忆 + 报告存储） |
| Web 框架 | FastAPI |
| 配置管理 | config.yaml + Pydantic Settings |
| 测试 | pytest（覆盖率 ≥ 85%） |
| Python | ≥ 3.11 |

## 项目结构
```
eragent/
├── api/                         # REST API 层
│   ├── main.py                  # FastAPI 应用入口
│   ├── routes/analyze.py        # 分析路由
│   └── schemas/analysis.py      # Pydantic 请求/响应模型
├── config/
│   ├── config.yaml              # 结构化配置文件
│   └── settings.py              # Pydantic Settings 配置管理
├── core/                        # 核心基础设施
│   ├── ontology/                # OWL 本体加载和推理
│   │   ├── loader.py            # Owlready2 本体加载器
│   │   └── reasoner.py          # 推理器 + SWRL 规则定义
│   ├── knowledge/               # 知识存储
│   │   ├── graph.py             # Neo4j 图数据库封装
│   │   └── vector_store.py      # Chroma 向量存储封装
│   ├── orchestrator/            # 编排控制层
│   │   ├── intent.py            # 意图解析（关键词匹配）
│   │   └── orchestrator.py      # 分析任务编排器
│   └── memory.py                # 短期/长期记忆管理
├── modules/p2p/                 # P2P 业务模块
│   ├── rules/                   # 业务规则引擎（__init__.py 提供统一导出）
│   │   ├── three_way_match.py   # 三路匹配异常检测
│   │   ├── price_variance.py    # 价格差异分析
│   │   ├── payment_compliance.py # 付款合规检查
│   │   └── supplier_performance.py # 供应商绩效 KPI
│   ├── ontology/p2p.owl         # P2P 领域 OWL 本体（非 Python 包）
│   ├── tools.py                 # LangChain @tool 工具集（8个）
│   ├── agent.py                 # P2P Agent（create_agent）
│   └── mock_data/generator.py   # 模拟数据生成器
├── docs/                        # 项目文档
│   ├── erp_agent_spec.md        # 系统设计规格
│   └── erp_procurement_agent.pdf # 采购分析需求文档
├── tests/                       # 测试（无 __init__.py）
│   ├── conftest.py              # 共享 fixture
│   ├── unit/                    # 单元测试
│   └── integration/             # 集成测试（含 test_e2e.py 端到端）
├── .env                         # 环境变量（不提交）
├── .env.example                 # 环境变量模板
└── pyproject.toml               # 项目配置
```

## Import 路径约定
所有模块使用**不带 `eragent.` 前缀**的绝对导入，项目根目录（`eragent/`）在 `sys.path` 中：
```python
from config.settings import Settings, get_settings
from api.schemas.analysis import AnalysisResult, Severity
from core.ontology.reasoner import OntologyReasoner
from modules.p2p.rules.three_way_match import ThreeWayMatchChecker
```

## 关键设计决策
- **编排粒度**：MVP 采用粗粒度——Orchestrator 路由到 P2P Agent，Agent 内部串行处理。预留接口支持后续细粒度 DAG 调度。
- **本体上下文注入**：混合模式——关键规则用结构化 JSON，业务背景用自然语言。
- **SWRL 规则 vs Python 代码**：合规规则（三路匹配、付款条款）用 SWRL 定义于本体；KPI 计算用 Python 实现。
- **MVP 纯分析只读**：不执行 ERP 写操作，写操作接口预留。
- **记忆隔离**：长期记忆按 `user_id` 隔离，短期记忆按 `session_id` 隔离。
- **上下文压缩**：摘要压缩 + 语义检索混合模式。

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
- `modules/p2p/rules/__init__.py` 提供四个规则类的统一导出，为唯一有实际内容的 `__init__.py`

## 当前进度
- 所有功能模块代码已完成
- 单元测试 + 集成测试全部通过，覆盖率 ≥ 95%
- 端到端测试（真实 LLM）通过
- 日志模块（structlog）已删除，等功能验证后补充
