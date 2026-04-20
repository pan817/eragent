# 测试策略

## 1. 单元测试（10 个文件）

| 测试文件 | 覆盖范围 |
|---------|---------|
| test_etl_extractors.py | 各域 Extractor 的全量/增量抽取逻辑，Mock DB session |
| test_etl_transformers.py | StructuredTransformer 映射规则正确性（每张表至少 1 个 case） |
| test_etl_llm_extractor.py | LLM 抽取 prompt 构建、结果解析、开关控制 |
| test_etl_loader.py | GraphitiLoader 写入逻辑、幂等性、批量处理、错误处理 |
| test_etl_state.py | SyncStateManager CRUD、水位线判断、全量/增量切换 |
| test_etl_scheduler.py | 调度器启动/停机/手动触发、并发锁 |
| test_etl_pipeline.py | Pipeline 编排顺序、域依赖、超时处理、部分失败 |
| test_etl_config.py | GraphitiETLSettings 默认值、env 覆盖、校验 |
| test_graphiti_client.py | GraphitiClient 初始化、连接、search、add_episode |
| test_graph_tools.py | 6 个图查询工具的参数校验、返回格式、错误处理 |

### 单元测试要点

- **Extractor**：Mock `AsyncSession`，验证 SQL WHERE 条件和分批逻辑
- **Transformer**：纯函数测试，输入一行 EBS 数据 → 验证输出的 node/edge 结构
- **Loader**：Mock GraphitiClient，验证调用参数和幂等行为
- **Scheduler**：Mock Pipeline，验证定时触发和并发锁
- **映射规则**：20 张表每张至少 1 个正向 case + 1 个边界 case（缺失字段、None 值）

## 2. 集成测试（3 个文件）

| 测试文件 | 覆盖范围 |
|---------|---------|
| test_etl_e2e.py | 完整 ETL 流程：写入 EBS 测试数据 → 全量同步 → 验证图谱节点/边 → 增量同步 → 验证变更 |
| test_graph_query_tools.py | 图查询工具端到端：预置图谱数据 → 调用 tool → 验证返回结果 |
| test_etl_scheduler_lifecycle.py | 调度器生命周期：启动 → 触发同步 → 优雅停机 |

### 集成测试要点

- 需要真实 Neo4j 实例（Docker testcontainers 或 CI 中的 Neo4j 服务）
- 需要真实 PostgreSQL（复用现有测试基础设施）
- LLM 抽取测试使用 FakeEmbeddingProvider + Mock LLM
- 每个测试前清空 Neo4j 测试数据库

## 3. 测试 Fixture

```python
# tests/fixtures/etl.py

@pytest.fixture
def ebs_test_data(db_session) -> dict:
    """插入一组最小完整的 EBS 测试数据
    （1 供应商 + 2 PO + 收货 + 发票 + 付款 + 1 合同）"""

@pytest.fixture
def mock_graphiti_client():
    """Mock GraphitiClient，记录所有写入调用"""

@pytest.fixture
def etl_pipeline(db_session, mock_graphiti_client) -> ETLPipeline:
    """构建测试用 Pipeline（Mock 依赖）"""

@pytest.fixture
async def neo4j_test_db():
    """真实 Neo4j 测试数据库（集成测试用），测试后清空"""
```

Fixture 文件放在 `tests/fixtures/etl.py`，通过 `conftest.py` 的 `pytest_plugins` 引用。

## 4. 覆盖率目标

- 单元测试：≥ 90%（与项目现有标准一致）
- 集成测试：核心流程覆盖（全量同步、增量同步、图查询）
- 映射规则：20 张表每张至少 1 个正向 + 1 个边界 case
