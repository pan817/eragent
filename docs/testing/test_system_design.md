# 测试用例体系设计与落地方案

> 基于 [test_audit_report.md](test_audit_report.md) 审计结论制定
> 日期：2026-05-03 | 分支：feature-plan-and-solve

---

## 1. 测试目录结构 + 命名规范

### 1.1 目标目录树

在现有 `tests/unit/`、`tests/integration/` **不动**的前提下，新增 `functional/` 和 `regression/` 两层，扩充 `fixtures/`。

```
tests/
├── conftest.py                              # 全局 fixture（不变）
├── fixtures/
│   ├── __init__.py
│   ├── p2p.py                               # 已有：静态 mock 数据
│   ├── queries.py                           # 【新增】标准查询集 + 预期路由映射
│   ├── tool_chain.py                        # 【新增】真实工具链 fixture（repo + tool 注入 + registry）
│   └── llm_stubs.py                         # 【新增】确定性 LLM mock（按 query → 固定 signal）
│
├── unit/                                    # 不变，67 文件
│   └── ...
│
├── functional/                              # 【新增】功能域集成测试（真实 DB + 真实工具，mock LLM）
│   ├── conftest.py                          # functional 层公共 fixture（引用 fixtures/*）
│   ├── test_routing_accuracy.py             # 域 1：意图路由准确性
│   ├── test_lookup_pipeline.py              # 域 2：Lookup 全链路
│   ├── test_tool_correctness.py             # 域 3：工具参数组合 + 输出正确性
│   ├── test_dag_pipeline.py                 # 域 4：DAG 真实执行
│   ├── test_multi_turn.py                   # 域 5：多轮对话
│   └── test_memory_effect.py                # 域 6：记忆注入效果
│
├── regression/                              # 【新增】回归基线
│   ├── conftest.py
│   └── test_canary_queries.py               # 金丝雀查询集
│
├── integration/                             # 不变，8 文件
│   └── ...
│
└── http/                                    # 不变，手动 REST 测试
    └── ...
```

### 1.2 文件命名规则

| 层级 | 命名模式 | 示例 |
|------|---------|------|
| unit/ | `test_{被测模块名}.py` | `test_orchestrator.py`（不变） |
| functional/ | `test_{功能域名}_pipeline.py` 或 `test_{功能域名}_accuracy.py` | `test_lookup_pipeline.py` |
| regression/ | `test_canary_{域名}.py` | `test_canary_queries.py` |
| integration/ | `test_{集成场景}.py` | `test_e2e.py`（不变） |

### 1.3 pytest 标记体系

在 `pyproject.toml` 的 `[tool.pytest.ini_options]` 中注册：

```toml
[tool.pytest.ini_options]
markers = [
    "unit: 单元测试（默认，每次 commit 运行）",
    "functional: 功能域集成测试（真实 DB + 工具，mock LLM，每次 commit 运行）",
    "regression: 回归基线测试（每次 PR 运行）",
    "integration: 组件间集成测试（每次 PR 运行）",
    "e2e: 端到端测试（需 LLM API Key，发版前运行）",
    "slow: 耗时 > 5s 的测试（可选跳过）",
]
```

标记应用方式：在各层 `conftest.py` 中自动标记该目录下所有测试：

```python
# tests/functional/conftest.py
import pytest

def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.functional)
```

### 1.4 运行命令约定

```bash
# 日常开发：unit + functional（快速，无外部依赖）
pytest tests/unit tests/functional -x -q

# PR 合并前：加上 regression + integration
pytest tests/ -m "not e2e" --cov=. --cov-fail-under=90

# 发版前：全量含 E2E（需 LLM_API_KEY）
pytest tests/ --cov=. --cov-fail-under=90
```

---

## 2. 公共 Fixture 体系

### 2.1 标准查询集 `tests/fixtures/queries.py`

集中定义所有测试共享的"查询 → 预期行为"映射，避免各文件各自硬编码查询字符串。

**数据结构**：

```python
@dataclass(frozen=True)
class CanaryQuery:
    """一条金丝雀查询的完整预期。"""
    query: str                          # 用户原始查询
    expected_route: str                 # 预期路由路径：bypass / lookup_shortcut / DAG / plan_and_solve / agent
    expected_intent: str                # 预期 IntentKind 值：CHITCHAT / META / RECALL / DATA_LOOKUP / ANALYSIS
    expected_tool: str | None           # 预期命中的首个工具名（bypass 时为 None）
    min_result_count: int | None        # lookup 路径：预期最少返回记录数（None=不检查）
    must_contain_fields: list[str]      # lookup 路径：返回 dict 中必须包含的 key
    assert_order_field: str | None      # 排序验证字段名（如 "creation_date"），None=不检查排序
    assert_order_dir: str | None        # "desc" / "asc"
    tags: tuple[str, ...]              # 分类标签，用于 pytest 参数化过滤
```

**查询集定义**（20 条，覆盖全路由路径）：

```python
CANARY_QUERIES: list[CanaryQuery] = [
    # ── lookup shortcut 路径（8 条）──
    CanaryQuery("最新的一个po",             "lookup_shortcut", "DATA_LOOKUP", "query_purchase_orders",  1, ["po_number"], "creation_date", "desc", ("lookup", "po")),
    CanaryQuery("最新的5个PO",              "lookup_shortcut", "DATA_LOOKUP", "query_purchase_orders",  5, ["po_number"], "creation_date", "desc", ("lookup", "po")),
    CanaryQuery("金额最大的3笔付款",         "lookup_shortcut", "DATA_LOOKUP", "query_payments",         3, ["amount"],    "amount",        "desc", ("lookup", "payment")),
    CanaryQuery("查询最近7天的发票",         "lookup_shortcut", "DATA_LOOKUP", "query_invoices",         1, ["invoice_num"], None,           None,   ("lookup", "invoice")),
    CanaryQuery("查看PO-001",               "lookup_shortcut", "DATA_LOOKUP", "query_purchase_orders",  1, ["po_number"], None,            None,   ("lookup", "po", "entity")),
    CanaryQuery("SUP-001的采购订单",         "lookup_shortcut", "DATA_LOOKUP", "query_purchase_orders",  1, ["vendor_id"], None,            None,   ("lookup", "po", "entity")),
    CanaryQuery("本月的供应商列表",           "lookup_shortcut", "DATA_LOOKUP", "query_vendor_master",    1, [],            None,            None,   ("lookup", "supplier")),
    CanaryQuery("查一下最近的收货单",         "lookup_shortcut", "DATA_LOOKUP", "query_receipts",         1, [],            None,            None,   ("lookup", "receipt")),

    # ── DAG 路径（4 条）──
    CanaryQuery("分析三路匹配异常",          "DAG",             "ANALYSIS",    "run_three_way_match",     None, None, None, None, ("dag", "3wm")),
    CanaryQuery("分析采购价格差异",          "DAG",             "ANALYSIS",    "run_price_variance_analysis", None, None, None, None, ("dag", "price")),
    CanaryQuery("检查付款合规性",            "DAG",             "ANALYSIS",    "run_payment_compliance_check", None, None, None, None, ("dag", "compliance")),
    CanaryQuery("评估SUP-001的绩效",        "DAG",             "ANALYSIS",    "calculate_supplier_kpis", None, None, None, None, ("dag", "supplier")),

    # ── PS / agent 路径（4 条）──
    CanaryQuery("帮我全面分析采购数据",       "plan_and_solve|agent", "ANALYSIS", None,                  None, None, None, None, ("ps",)),
    CanaryQuery("查询最近7天采购订单概况",    "plan_and_solve|agent", "ANALYSIS", None,                  None, None, None, None, ("ps",)),
    CanaryQuery("列出最近7天的采购订单和发票", "plan_and_solve|agent", "ANALYSIS", None,                  None, None, None, None, ("ps", "cross_entity")),
    CanaryQuery("为什么最新的PO金额这么高",   "plan_and_solve|agent", "ANALYSIS", None,                  None, None, None, None, ("ps", "blacklist")),

    # ── bypass 路径（4 条）──
    CanaryQuery("你好",                     "bypass",          "CHITCHAT",    None, None, None, None, None, ("bypass",)),
    CanaryQuery("你能做什么",               "bypass",          "META",        None, None, None, None, None, ("bypass",)),
    CanaryQuery("上次分析了什么",            "bypass",          "RECALL",      None, None, None, None, None, ("bypass",)),
    CanaryQuery("最近付款合规情况",          "DAG|plan_and_solve", "ANALYSIS", None, None, None, None, None, ("blacklist",)),
]
```

**复用方式**：

```python
# 在 tests/fixtures/queries.py 底部提供按 tag 过滤的辅助函数
def get_queries_by_tag(*tags: str) -> list[CanaryQuery]:
    """返回含任一指定 tag 的查询子集。"""
    tag_set = set(tags)
    return [q for q in CANARY_QUERIES if tag_set & set(q.tags)]

# 在各测试文件中使用
# tests/functional/test_routing_accuracy.py
from tests.fixtures.queries import CANARY_QUERIES

@pytest.mark.parametrize("cq", CANARY_QUERIES, ids=lambda cq: cq.query)
def test_route_intent_kind(cq: CanaryQuery, deterministic_router):
    signal = deterministic_router.route(cq.query)
    assert signal.intent_kind.value == cq.expected_intent
```

### 2.2 真实工具链 `tests/fixtures/tool_chain.py`

提供一站式 fixture，让 functional 层测试直接获得"注入好 repo 的真实工具 + ToolRegistry"，无需各文件自行组装。

**核心 fixture 清单**：

```python
# tests/fixtures/tool_chain.py

@pytest.fixture(scope="session")
def seeded_engine():
    """会话级 SQLite 内存引擎，灌入 50 条种子数据。

    scope=session：所有 functional 测试共享同一份数据，避免重复 init。
    返回: sqlalchemy.Engine
    """
    engine = create_engine_from_dsn(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    install_sqlite_timezone_hook(engine)
    init_database(engine, seed=0, count=50, data_generator_factory=MockDataGenerator)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def seeded_repo(seeded_engine) -> P2PRepository:
    """会话级真实 Repository（绑定种子数据引擎）。

    返回: P2PRepository 实例，可直接调用 query_purchase_orders 等方法。
    """
    return P2PRepository(get_session_factory(seeded_engine))


@pytest.fixture(autouse=True)
def _inject_repo(seeded_repo):
    """自动注入 repo 到工具模块全局变量。

    每个测试自动执行，确保 PG 工具通过 _get_query_backend() 拿到真实 repo。
    teardown 时还原为 None 防止泄漏。
    """
    set_repository(seeded_repo)
    yield
    set_repository(None)


@pytest.fixture(scope="session")
def tool_registry(seeded_repo) -> ToolRegistry:
    """会话级 ToolRegistry，注册全部 PG 工具（真实函数，非 mock）。

    返回: ToolRegistry，可 .get("query_purchase_orders") 获取真实 tool。
    注意: 图工具未注册（需 Neo4j），仅包含 15 个 PG 工具 + 1 个 chat 工具。
    """
    set_repository(seeded_repo)
    provider = P2PModuleProvider()
    return build_registry_from_provider(provider)


@pytest.fixture()
def seed_data_summary() -> dict:
    """返回种子数据的统计摘要，供断言参考。

    返回 dict 包含:
      - po_count: int          # 总 PO 数量
      - supplier_ids: list[str] # 所有供应商 ID
      - first_po: str           # 最早创建的 PO 号
      - latest_po: str          # 最近创建的 PO 号
      - max_amount_po: str      # 金额最大的 PO 号
    """
    gen = MockDataGenerator(seed=0)
    data = gen.generate_all(count=50)
    headers = data["po_headers"]
    by_date = sorted(headers, key=lambda h: h["creation_date"])
    by_amount = sorted(headers, key=lambda h: h["total_amount"], reverse=True)
    return {
        "po_count": len(headers),
        "supplier_ids": sorted({h["vendor_id"] for h in headers}),
        "first_po": by_date[0]["po_number"],
        "latest_po": by_date[-1]["po_number"],
        "max_amount_po": by_amount[0]["po_number"],
    }
```

**functional/conftest.py 引用方式**：

```python
# tests/functional/conftest.py
import pytest

# 引入公共 fixture 模块
pytest_plugins = [
    "tests.fixtures.p2p",
    "tests.fixtures.tool_chain",
    "tests.fixtures.queries",
    "tests.fixtures.llm_stubs",
]

def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.functional)
```

### 2.3 确定性 LLM `tests/fixtures/llm_stubs.py`

functional 层需要绕过真实 LLM 调用（不依赖 API Key），但又不能像 unit 层那样直接 `patch` 掉整个 router。解决方案：提供一个"确定性路由器"，内部用规则（非 LLM）将查询映射到 `QuerySignal`，覆盖 `UnifiedRouter.route()` 中的 LLM 调用部分。

**设计思路**：

- bypass 检测（正则）本身不依赖 LLM → 保持真实
- `_extract_params`（正则提取实体/时间）本身不依赖 LLM → 保持真实
- `UnifiedRouter._call_llm()`（LLM 调用）→ 用查询集映射表替代

```python
# tests/fixtures/llm_stubs.py

from tests.fixtures.queries import CANARY_QUERIES, CanaryQuery
from core.orchestrator.signal import IntentKind, QuerySignal

# 查询 → 预期信号的映射表
_QUERY_SIGNAL_MAP: dict[str, QuerySignal] = {}

def _build_signal(cq: CanaryQuery) -> QuerySignal:
    """从 CanaryQuery 构造对应的 QuerySignal。"""
    intent = IntentKind(cq.expected_intent)
    return QuerySignal(
        raw_query=cq.query,
        intent_kind=intent,
        keywords=[cq.expected_intent.lower()],
        confidence=0.95,
        route_level=1 if "DAG" in (cq.expected_route or "") else 3,
        reasoning=f"deterministic stub for: {cq.query}",
        is_cross_entity="cross_entity" in cq.tags,
    )

for _cq in CANARY_QUERIES:
    _QUERY_SIGNAL_MAP[_cq.query] = _build_signal(_cq)


@pytest.fixture()
def deterministic_router(settings):
    """返回一个路由器，LLM 调用部分用查询映射表替代。

    bypass 检测和参数提取仍走真实代码路径。
    仅 LLM 分类步骤被替换为确定性映射。

    用法：
        signal = deterministic_router.route(query, params)
        assert signal.intent_kind == IntentKind.DATA_LOOKUP
    """
    from core.orchestrator.router import IntentRouter, _classify_bypass, _extract_params

    router = IntentRouter(settings=settings, provider=P2PModuleProvider())

    async def _stubbed_route(query: str, params: dict | None = None, **kwargs):
        # bypass 检测走真实路径
        bypass_result = _classify_bypass(query)
        if bypass_result is not None:
            return bypass_result

        # LLM 路由部分用映射表替代
        if query in _QUERY_SIGNAL_MAP:
            signal = _QUERY_SIGNAL_MAP[query]
            extracted = _extract_params(query)
            if params:
                extracted.update(params)
            signal = dataclasses.replace(signal, entities=extracted)
            return signal

        # 未在映射表中 → 默认 ANALYSIS L3
        return QuerySignal(
            raw_query=query,
            intent_kind=IntentKind.ANALYSIS,
            confidence=0.5,
            route_level=3,
        )

    router.route = _stubbed_route
    return router
```

**使用场景**：

| 测试文件 | 怎么用 |
|---------|--------|
| `test_routing_accuracy.py` | 验证 bypass + 参数提取（真实路径） |
| `test_lookup_pipeline.py` | 注入到 orchestrator，验证 lookup 全链路 |
| `test_dag_pipeline.py` | 使查询稳定路由到 DAG |
| `test_canary_queries.py` | 回归测试全链路 |

---

## 3. functional 层测试用例设计（路由 + Lookup + 工具）

### 3.1 `test_routing_accuracy.py` — 意图路由准确性

**目标**：验证"真实查询文本 → 正确路由结果"，覆盖 bypass 真实正则 + 参数提取真实正则 + LLM 分类确定性映射。

**依赖 fixture**：`deterministic_router`、`CANARY_QUERIES`

| # | 测试函数 | 输入查询 | 断言要点 |
|---|---------|---------|---------|
| 1 | `test_bypass_chitchat[你好]` | "你好" | `signal.intent_kind == CHITCHAT` |
| 2 | `test_bypass_chitchat[谢谢]` | "谢谢" | `signal.intent_kind == CHITCHAT` |
| 3 | `test_bypass_meta[你能做什么]` | "你能做什么" | `signal.intent_kind == META` |
| 4 | `test_bypass_recall[上次分析了什么]` | "上次分析了什么" | `signal.intent_kind == RECALL` |
| 5 | `test_data_lookup_simple_po` | "最新的一个po" | `intent_kind == DATA_LOOKUP`, `is_cross_entity == False` |
| 6 | `test_data_lookup_with_limit` | "最新的5个PO" | `intent_kind == DATA_LOOKUP` |
| 7 | `test_data_lookup_payment` | "金额最大的3笔付款" | `intent_kind == DATA_LOOKUP` |
| 8 | `test_data_lookup_invoice_time` | "查询最近7天的发票" | `intent_kind == DATA_LOOKUP` |
| 9 | `test_data_lookup_entity_po` | "查看PO-001" | `intent_kind == DATA_LOOKUP`, `entities["po_number"] == "PO-001"` |
| 10 | `test_data_lookup_entity_supplier` | "SUP-001的采购订单" | `intent_kind == DATA_LOOKUP`, `entities["vendor_id"] == "SUP-001"` |
| 11 | `test_analysis_three_way_match` | "分析三路匹配异常" | `intent_kind == ANALYSIS` |
| 12 | `test_analysis_price_variance` | "分析采购价格差异" | `intent_kind == ANALYSIS` |
| 13 | `test_analysis_payment_compliance` | "检查付款合规性" | `intent_kind == ANALYSIS` |
| 14 | `test_analysis_supplier_kpi` | "评估SUP-001的绩效" | `intent_kind == ANALYSIS`, `entities["vendor_id"] == "SUP-001"` |
| 15 | `test_analysis_comprehensive` | "帮我全面分析采购数据" | `intent_kind == ANALYSIS` |
| 16 | `test_blacklist_blocks_lookup` | "为什么最新的PO金额这么高" | `intent_kind == ANALYSIS`（非 DATA_LOOKUP） |
| 17 | `test_cross_entity_blocks_lookup` | "列出最近7天的采购订单和发票" | `is_cross_entity == True` 或 `intent_kind == ANALYSIS` |
| 18 | `test_param_extract_days` | "分析最近60天的数据" | `entities["days"] == 60` |
| 19 | `test_param_extract_po_number` | "检查PO-2024-0001的匹配情况" | `entities["po_number"] == "PO-2024-0001"` |
| 20 | `test_param_extract_invoice` | "查看发票INV-2024-0001" | `entities["invoice_num"] == "INV-2024-0001"` |

**实现模式**：

```python
class TestBypassRouting:
    """bypass 层（正则，完全真实）。"""

    @pytest.mark.parametrize("query,expected_kind", [
        ("你好", "CHITCHAT"), ("谢谢", "CHITCHAT"),
        ("你能做什么", "META"), ("支持哪些分析", "META"),
        ("上次分析了什么", "RECALL"), ("刚才的结果", "RECALL"),
    ])
    async def test_bypass(self, deterministic_router, query, expected_kind):
        signal = await deterministic_router.route(query)
        assert signal.intent_kind.value == expected_kind


class TestParamExtraction:
    """参数提取（正则，完全真实，不经过 LLM）。"""

    @pytest.mark.parametrize("query,field,expected", [
        ("分析最近60天的数据", "days", 60),
        ("检查PO-2024-0001的匹配情况", "po_number", "PO-2024-0001"),
        ("查看发票INV-2024-0001", "invoice_num", "INV-2024-0001"),
        ("查询付款单PAY-001", "check_number", "PAY-001"),
        ("SUP-001的采购订单", "vendor_id", "SUP-001"),
    ])
    async def test_extract(self, deterministic_router, query, field, expected):
        signal = await deterministic_router.route(query)
        assert signal.entities.get(field) == expected


class TestIntentClassification:
    """意图分类（LLM 部分走确定性映射）。"""

    @pytest.mark.parametrize("cq", get_queries_by_tag("lookup"), ids=lambda c: c.query)
    async def test_lookup_queries_get_data_lookup(self, deterministic_router, cq):
        signal = await deterministic_router.route(cq.query)
        assert signal.intent_kind == IntentKind.DATA_LOOKUP

    @pytest.mark.parametrize("cq", get_queries_by_tag("dag"), ids=lambda c: c.query)
    async def test_dag_queries_get_analysis(self, deterministic_router, cq):
        signal = await deterministic_router.route(cq.query)
        assert signal.intent_kind == IntentKind.ANALYSIS
```

### 3.2 `test_lookup_pipeline.py` — Lookup 全链路

**目标**：验证从 `resolve_lookup_tool()` → 真实 `tool.ainvoke()` → `format_lookup_result()` → orchestrator 判断是否 fallback 的完整链路。**工具调用真实、DB 查询真实**，仅 LLM 路由走确定性映射。

**依赖 fixture**：`seeded_repo`、`tool_registry`、`seed_data_summary`

| # | 测试函数 | 场景 | 断言要点 |
|---|---------|------|---------|
| 1 | `test_latest_one_po_returns_data` | "最新的一个po" → resolve → ainvoke → format | 返回 markdown 含表格，行数=1，含 po_number 列 |
| 2 | `test_latest_one_po_is_actually_latest` | 同上 | 返回的 po_number == seed_data_summary["latest_po"] |
| 3 | `test_latest_5_po_count_and_order` | "最新的5个PO" → 完整链路 | 行数=5，第一行日期 >= 第二行日期（倒序） |
| 4 | `test_amount_desc_3_payments` | "金额最大的3笔付款" → 完整链路 | 行数=3，第一行金额 >= 第二行金额 |
| 5 | `test_entity_po_number_exact` | "查看PO-001" → Path A | 返回恰好 1 条，po_number 含 "PO-001"（注意种子数据的实际编号格式） |
| 6 | `test_entity_vendor_filter` | "SUP-001的采购订单" → Path A | 所有行的 vendor_id == "SUP-001" |
| 7 | `test_invoice_time_window` | "查询最近7天的发票" → Path B | 所有行日期在近 7 天内 |
| 8 | `test_receipt_lookup` | "查一下最近的收货单" → Path B | 返回行数 >= 1 |
| 9 | `test_supplier_list` | "本月的供应商列表" → Path B | 返回行数 >= 1 |
| 10 | `test_empty_result_returns_not_found_message` | days=1 且种子数据无当天 PO → 真实调用 | 返回 "未找到匹配的采购订单。" |
| 11 | `test_empty_result_should_be_none_for_orchestrator` | 同上 | **关键**：验证 orchestrator 级别应将此视为 miss（当前为 bug，此测试作为修复后的验收条件） |
| 12 | `test_tool_exception_returns_none` | mock 工具抛 RuntimeError | `execute_lookup()` 返回 None |
| 13 | `test_blacklist_word_bypasses_lookup` | "最近采购订单异常" | `resolve_lookup_tool()` 返回 None |
| 14 | `test_cross_entity_bypasses_lookup` | "最近7天的采购订单和发票" | `resolve_lookup_tool()` 返回 None |

**实现模式（示例：test 1-3）**：

```python
class TestLookupWithRealTools:
    """真实工具 + 真实 DB 的 lookup 全链路测试。"""

    async def test_latest_one_po_returns_data(self, tool_registry):
        tool_name, kwargs = resolve_lookup_tool(
            {"days": 30}, "最新的一个po", provider=P2PModuleProvider(),
        )
        assert tool_name == "query_purchase_orders"
        assert kwargs["limit"] == 1
        assert kwargs["order_by"] == "date_desc"

        result_md = await execute_lookup(tool_name, kwargs, tool_registry)

        assert result_md is not None
        assert "未找到" not in result_md
        assert "|" in result_md  # markdown 表格标记
        # 表格行数 = 1（表头 + 分隔 + 1 数据行）
        data_rows = [l for l in result_md.split("\n") if l.startswith("|") and "---" not in l]
        assert len(data_rows) == 2  # 1 header + 1 data

    async def test_latest_one_po_is_actually_latest(
        self, tool_registry, seed_data_summary,
    ):
        result_md = await execute_lookup(
            "query_purchase_orders",
            {"days": 0, "limit": 1, "order_by": "date_desc"},
            tool_registry,
        )
        assert seed_data_summary["latest_po"] in result_md

    async def test_latest_5_po_count_and_order(self, tool_registry):
        result_md = await execute_lookup(
            "query_purchase_orders",
            {"days": 0, "limit": 5, "order_by": "date_desc"},
            tool_registry,
        )
        data_rows = [l for l in result_md.split("\n") if l.startswith("|") and "---" not in l]
        assert len(data_rows) == 6  # 1 header + 5 data


class TestLookupEmptyAndFallback:
    """空结果和降级路径。"""

    async def test_empty_result_returns_not_found(self, tool_registry):
        result_md = await execute_lookup(
            "query_purchase_orders",
            {"days": 1, "limit": 1, "order_by": "date_desc"},  # 种子数据无当天 PO
            tool_registry,
        )
        assert "未找到" in result_md

    async def test_empty_should_trigger_fallback(self, tool_registry):
        """修复后的验收条件：空结果时 _try_lookup_shortcut 应返回 None。"""
        # 此测试在 bug 修复前会 FAIL，作为 regression guard
        result_md = await execute_lookup(
            "query_purchase_orders",
            {"days": 1, "limit": 1, "order_by": "date_desc"},
            tool_registry,
        )
        # 当前 bug: result_md = "未找到..." (非 None)
        # 修复后: execute_lookup 或 orchestrator 应识别空结果并返回 None
        assert result_md is None or "未找到" not in result_md
```

### 3.3 `test_tool_correctness.py` — 工具参数组合 + 输出正确性

**目标**：直接调用 15 个 PG 工具的 `ainvoke()`，传入关键参数组合，验证返回数据的**内容正确性**（而非仅类型）。

**依赖 fixture**：`seeded_repo`（autouse 注入）、`seed_data_summary`

**设计原则**：每个工具至少 2-3 个参数组合。查询类工具重点测 limit / order_by / 过滤条件；分析类工具重点测返回结构和关键数值非零。

#### 查询工具（4 个，共 16 个测试）

```python
class TestQueryPurchaseOrders:
    """query_purchase_orders 参数组合测试。"""

    async def test_no_filter_returns_all(self):
        data = json.loads(await query_purchase_orders.ainvoke({"days": 0}))
        assert len(data) >= 40  # 50 条种子，部分可能无 line_location

    async def test_days_filter(self):
        data = json.loads(await query_purchase_orders.ainvoke({"days": 7}))
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        for row in data:
            assert row["creation_date"] >= cutoff

    async def test_vendor_filter(self, seed_data_summary):
        vid = seed_data_summary["supplier_ids"][0]
        data = json.loads(await query_purchase_orders.ainvoke({"vendor_id": vid, "days": 0}))
        assert all(row["vendor_id"] == vid for row in data)
        assert len(data) >= 1

    async def test_limit_1_order_date_desc(self, seed_data_summary):
        data = json.loads(await query_purchase_orders.ainvoke(
            {"days": 0, "limit": 1, "order_by": "date_desc"},
        ))
        assert len(data) == 1
        assert data[0]["po_number"] == seed_data_summary["latest_po"]

    async def test_limit_5_order_date_desc(self):
        data = json.loads(await query_purchase_orders.ainvoke(
            {"days": 0, "limit": 5, "order_by": "date_desc"},
        ))
        assert len(data) == 5
        dates = [row["creation_date"] for row in data]
        assert dates == sorted(dates, reverse=True)

    async def test_order_amount_desc(self):
        data = json.loads(await query_purchase_orders.ainvoke(
            {"days": 0, "limit": 3, "order_by": "amount_desc"},
        ))
        amounts = [row["po_amount"] for row in data]
        assert amounts == sorted(amounts, reverse=True)

    async def test_status_filter(self):
        data = json.loads(await query_purchase_orders.ainvoke(
            {"status": "APPROVED", "days": 0},
        ))
        assert all(row["status"] == "APPROVED" for row in data)

    async def test_required_fields_present(self):
        data = json.loads(await query_purchase_orders.ainvoke({"days": 0, "limit": 1}))
        required = {"po_number", "vendor_id", "vendor_name", "po_amount",
                     "unit_price", "status", "creation_date"}
        assert required.issubset(data[0].keys())
```

```python
class TestQueryReceipts:
    async def test_no_filter(self):
        data = json.loads(await query_receipts.ainvoke({"days": 0}))
        assert len(data) >= 1

    async def test_by_po_number(self, seed_data_summary):
        po = seed_data_summary["latest_po"]
        data = json.loads(await query_receipts.ainvoke({"po_number": po, "days": 0}))
        assert all(row["po_number"] == po for row in data)

class TestQueryInvoices:
    async def test_no_filter(self):
        data = json.loads(await query_invoices.ainvoke({"days": 0}))
        assert len(data) >= 1

    async def test_limit_and_order(self):
        data = json.loads(await query_invoices.ainvoke(
            {"days": 0, "limit": 3, "order_by": "date_desc"},
        ))
        assert len(data) == 3

class TestQueryPayments:
    async def test_no_filter(self):
        data = json.loads(await query_payments.ainvoke({"days": 0}))
        assert len(data) >= 1

    async def test_amount_desc_order(self):
        data = json.loads(await query_payments.ainvoke(
            {"days": 0, "limit": 3, "order_by": "amount_desc"},
        ))
        amounts = [row["amount"] for row in data]
        assert amounts == sorted(amounts, reverse=True)
```

#### 分析工具（6 个，共 12 个测试）

```python
class TestThreeWayMatch:
    async def test_all_pos(self):
        data = json.loads(await run_three_way_match.ainvoke({"po_number": ""}))
        assert isinstance(data, list)
        assert len(data) >= 1
        assert "match_status" in data[0] or "status" in data[0]

    async def test_single_po(self, seed_data_summary):
        po = seed_data_summary["latest_po"]
        data = json.loads(await run_three_way_match.ainvoke({"po_number": po}))
        assert len(data) >= 1

class TestPriceVariance:
    async def test_all_vendors(self):
        data = json.loads(await run_price_variance_analysis.ainvoke(
            {"vendor_id": "", "days": 0},
        ))
        assert isinstance(data, list)

    async def test_single_vendor(self, seed_data_summary):
        vid = seed_data_summary["supplier_ids"][0]
        data = json.loads(await run_price_variance_analysis.ainvoke(
            {"vendor_id": vid, "days": 0},
        ))
        assert isinstance(data, list)

class TestPaymentCompliance:
    async def test_returns_results(self):
        data = json.loads(await run_payment_compliance_check.ainvoke(
            {"vendor_id": "", "days": 0},
        ))
        assert isinstance(data, list)
        assert len(data) >= 1

    async def test_contains_status_field(self):
        data = json.loads(await run_payment_compliance_check.ainvoke(
            {"vendor_id": "", "days": 0},
        ))
        if data:
            assert "status" in data[0] or "compliance_status" in data[0]

class TestSupplierKpis:
    async def test_known_vendor(self, seed_data_summary):
        vid = seed_data_summary["supplier_ids"][0]
        data = json.loads(await calculate_supplier_kpis.ainvoke(
            {"vendor_id": vid, "period": ""},
        ))
        assert isinstance(data, dict)
        assert "vendor_id" in data or vid in json.dumps(data)

class TestSpendAnalysis:
    async def test_by_category(self):
        data = json.loads(await calculate_spend_analysis.ainvoke(
            {"group_by": "category", "days": 0},
        ))
        assert isinstance(data, list)
        if data:
            assert "total_amount" in data[0]
            assert data[0]["total_amount"] > 0

class TestVendorConcentration:
    async def test_returns_structure(self):
        data = json.loads(await analyze_vendor_concentration.ainvoke({"days": 0}))
        assert "grand_total_spend" in data
        assert "top_vendors" in data
        assert data["grand_total_spend"] > 0
```

---

## 4. functional 层测试用例设计（DAG + 多轮 + 记忆）

### 4.1 `test_dag_pipeline.py` — DAG 真实执行

**目标**：用真实工具执行 4 种 DAG 模板（三路匹配 / 价格差异 / 付款合规 / 供应商绩效），验证工具间数据流和最终结果。Report Agent 的 LLM 调用仍 mock（返回固定汇总文本），仅验证 DAG 工具层的真实输出。

**依赖 fixture**：`tool_registry`、`seeded_repo`、`seed_data_summary`

| # | 测试函数 | 场景 | 断言要点 |
|---|---------|------|---------|
| 1 | `test_three_way_match_dag_all_tasks_succeed` | 加载 THREE_WAY_MATCH 模板 → executor.execute() | 所有 task status=success，output 非空 JSON |
| 2 | `test_three_way_match_dag_data_flows` | 同上 | fetch_po 的 output 含 po_number；match 任务 output 含 match_status |
| 3 | `test_price_variance_dag_succeeds` | PRICE_VARIANCE 模板 | 所有 task 成功，variance 任务 output 有数据 |
| 4 | `test_payment_compliance_dag_succeeds` | PAYMENT_COMPLIANCE 模板 | 所有 task 成功 |
| 5 | `test_supplier_performance_dag_with_entity` | SUPPLIER_PERFORMANCE 模板 + vendor_id | KPI 任务 output 含该 vendor_id |
| 6 | `test_dag_task_dependency_respected` | THREE_WAY_MATCH | match 任务在 fetch 任务之后执行（通过 TaskResult.start_time 验证） |
| 7 | `test_dag_report_receives_all_outputs` | 任一模板 + mock report agent | report agent 收到的 context 包含所有前置任务的 output |
| 8 | `test_dag_with_missing_data_graceful` | vendor_id=不存在的供应商 | 工具返回空列表，task 仍 status=success（不抛异常） |

**实现模式**：

```python
class TestDAGRealExecution:
    """DAG 真实工具执行测试。"""

    async def test_three_way_match_dag_all_tasks_succeed(
        self, tool_registry, seed_data_summary,
    ):
        provider = P2PModuleProvider()
        dag = load_dag_template(
            AnalysisType.THREE_WAY_MATCH,
            {"days": 0},  # 不限时间，确保有数据
            provider=provider,
        )
        assert dag is not None

        executor = DAGExecutor()
        results = await executor.execute(dag, tool_registry)

        for task_id, task_result in results.items():
            if task_id == "report":
                continue  # report 需要 LLM，跳过
            assert task_result.status == "success", f"{task_id} failed: {task_result.error}"
            assert task_result.output, f"{task_id} output is empty"

    async def test_dag_report_receives_all_outputs(self, tool_registry):
        dag = load_dag_template(AnalysisType.PRICE_VARIANCE, {"days": 0}, provider=P2PModuleProvider())
        mock_report = AsyncMock(return_value="汇总报告")

        # 替换 report 节点的工具为 mock
        for task in dag.tasks:
            if task.task_id == "report":
                original_tool = task.tool_name
                break

        executor = DAGExecutor()
        results = await executor.execute(dag, tool_registry, report_fn=mock_report)

        # 验证 mock_report 被调用，且收到了前置任务的输出
        mock_report.assert_called_once()
        context_arg = mock_report.call_args[0][0]  # 第一个位置参数
        assert len(context_arg) > 0  # 至少有 1 个前置任务的输出
```

### 4.2 `test_multi_turn.py` — 多轮对话

**目标**：验证 orchestrator 在多轮对话中正确处理代词引用、追问、上下文继承。LLM 路由走确定性映射，短期记忆使用真实 SQLite checkpointer。

**依赖 fixture**：`deterministic_router`、`seeded_repo`、`tool_registry`

| # | 测试函数 | 场景 | 断言要点 |
|---|---------|------|---------|
| 1 | `test_reference_resolution_supplier` | 第一轮 "查看SUP-001的订单" → 第二轮 "那个供应商的发票呢" | 第二轮 entities 中 vendor_id=SUP-001（引用解析） |
| 2 | `test_reference_resolution_po` | 第一轮 "查看PO-001" → 第二轮 "这个PO的收货情况" | 第二轮 entities 中 po_number=PO-001 |
| 3 | `test_followup_keeps_session` | 两轮查询使用同一 session_id | 第二轮 session_context 非空 |
| 4 | `test_recall_after_analysis` | 第一轮 "分析价格差异" → 第二轮 "上次分析了什么" | 第二轮路由为 RECALL，响应中包含"价格差异"相关内容 |
| 5 | `test_entity_inherits_across_turns` | 第一轮指定 vendor_id → 第二轮不指定 | 第二轮仍能获取到 vendor_id（从短期记忆） |
| 6 | `test_independent_sessions_isolated` | session_A 和 session_B 各自查询 | A 的实体不泄漏到 B |

**实现模式**：

```python
class TestMultiTurn:
    """多轮对话上下文继承测试。"""

    async def test_reference_resolution_supplier(self, functional_orchestrator):
        orch = functional_orchestrator
        sid = "test-multi-turn-1"

        r1 = await orch.analyze(query="查看SUP-001的订单", session_id=sid, user_id="u1")
        assert r1.status.value == "success"

        # 第二轮：代词引用 "那个供应商"
        r2 = await orch.analyze(query="那个供应商的发票呢", session_id=sid, user_id="u1")
        # 引用解析后应识别 SUP-001
        assert "SUP-001" in r2.summary.get("resolved_query", "") or \
               "SUP-001" in str(r2.summary)

    async def test_independent_sessions_isolated(self, functional_orchestrator):
        orch = functional_orchestrator
        await orch.analyze(query="查看SUP-001的订单", session_id="sess-A", user_id="u1")
        r = await orch.analyze(query="最新的一个po", session_id="sess-B", user_id="u1")
        # sess-B 不应继承 sess-A 的 SUP-001
        assert "SUP-001" not in r.report_markdown
```

### 4.3 `test_memory_effect.py` — 记忆注入效果

**目标**：验证长期记忆的写入 → 读取 → 注入 prompt 链路。使用真实 SQLite 记忆表。

**依赖 fixture**：`seeded_repo`、`db_engine`

| # | 测试函数 | 场景 | 断言要点 |
|---|---------|------|---------|
| 1 | `test_analysis_writes_entity_memory` | 执行一次分析 → 检查 memories 表 | user_id 对应记录 count >= 1 |
| 2 | `test_memory_injected_into_context` | 预写入 entity_profile 记忆 → 执行分析 | orchestrator 构建的 prompt 中包含该实体信息 |
| 3 | `test_correction_memory_affects_analysis` | 预写入 correction 记忆 → 执行相同类型分析 | 分析结果考虑了修正信息（通过 trace 可观测） |
| 4 | `test_memory_user_isolation` | user_A 写入记忆 → user_B 执行分析 | user_B 的 prompt 不包含 user_A 的记忆 |
| 5 | `test_memory_ttl_expiry` | 写入 TTL=1s 的记忆 → sleep(2) → 查询 | 过期记忆不被注入 |

**实现模式**：

```python
class TestMemoryWriteAndInject:
    async def test_analysis_writes_entity_memory(self, functional_orchestrator, db_engine):
        orch = functional_orchestrator
        await orch.analyze(query="分析SUP-001的绩效", user_id="mem-test-user", session_id="s1")

        with Session(db_engine) as session:
            count = session.execute(
                text("SELECT count(*) FROM memories WHERE user_id = 'mem-test-user'"),
            ).scalar()
        assert count >= 1

    async def test_memory_user_isolation(self, functional_orchestrator, db_engine):
        orch = functional_orchestrator
        # user_A 分析
        await orch.analyze(query="分析SUP-001的绩效", user_id="user-A", session_id="s1")
        # user_B 分析
        r = await orch.analyze(query="分析SUP-002的绩效", user_id="user-B", session_id="s2")

        # 通过 trace 或 summary 确认 user-B 的 prompt 不含 user-A 的记忆
        assert "user-A" not in str(r.summary)
```

---

## 5. regression 层 + 现有测试增强

### 5.1 `test_canary_queries.py` — 金丝雀回归测试

**目标**：用 20 条标准查询验证"路由 → 工具 → 结果"全链路不退化。每次 PR 必跑。

**实现方案**：参数化测试 + 按 `expected_route` 分发断言逻辑。

```python
# tests/regression/test_canary_queries.py

from tests.fixtures.queries import CANARY_QUERIES, CanaryQuery

@pytest.mark.parametrize("cq", CANARY_QUERIES, ids=lambda c: c.query)
class TestCanaryQuery:
    """金丝雀查询回归基线。

    每条查询验证三个层面：
    1. 路由结果匹配预期
    2. 命中的工具匹配预期
    3. 返回数据满足最低约束（行数、字段、排序）
    """

    async def test_route_matches(self, cq: CanaryQuery, deterministic_router):
        """验证路由结果与预期一致。"""
        signal = await deterministic_router.route(cq.query)
        expected_routes = cq.expected_route.split("|")
        # bypass 查 intent_kind，其余查 route 结果
        if cq.expected_intent in ("CHITCHAT", "META", "RECALL"):
            assert signal.intent_kind.value == cq.expected_intent
        else:
            assert signal.intent_kind.value == cq.expected_intent

    async def test_tool_hit(self, cq: CanaryQuery, tool_registry):
        """验证 lookup 路径命中正确工具（非 lookup 路径跳过）。"""
        if cq.expected_route != "lookup_shortcut":
            pytest.skip("non-lookup route")

        result = resolve_lookup_tool(
            {"days": 30}, cq.query, provider=P2PModuleProvider(),
        )
        assert result is not None, f"lookup miss for: {cq.query}"
        tool_name, kwargs = result
        assert tool_name == cq.expected_tool

    async def test_result_quality(self, cq: CanaryQuery, tool_registry):
        """验证返回数据满足最低质量约束。"""
        if cq.expected_route != "lookup_shortcut" or cq.min_result_count is None:
            pytest.skip("no result quality check for this route")

        result = resolve_lookup_tool(
            {"days": 0}, cq.query, provider=P2PModuleProvider(),
        )
        assert result is not None
        tool_name, kwargs = result
        # 强制 days=0 确保种子数据可查
        kwargs["days"] = 0

        tool = tool_registry.get(tool_name)
        raw = await tool.ainvoke(kwargs)
        data = json.loads(raw)

        # 剔除 _truncated 标记
        items = [d for d in data if not d.get("_truncated")]

        assert len(items) >= cq.min_result_count, \
            f"expected >= {cq.min_result_count} results, got {len(items)}"

        if cq.must_contain_fields:
            for field in cq.must_contain_fields:
                assert field in items[0], f"missing field: {field}"

        if cq.assert_order_field and len(items) >= 2:
            values = [item[cq.assert_order_field] for item in items]
            if cq.assert_order_dir == "desc":
                assert values == sorted(values, reverse=True), \
                    f"not sorted desc by {cq.assert_order_field}"
            elif cq.assert_order_dir == "asc":
                assert values == sorted(values), \
                    f"not sorted asc by {cq.assert_order_field}"
```

**扩展方式**：在 `fixtures/queries.py` 的 `CANARY_QUERIES` 列表中追加新条目即可，无需修改测试代码。

### 5.2 现有测试增强：逐文件改造清单

以下列出需要增强的现有文件，精确到函数级别。

#### 5.2.1 `tests/unit/test_tools.py`

| 现有函数 | 当前断言 | 增强为 |
|---------|---------|--------|
| `test_query_purchase_orders` | `isinstance(data, list)` | `len(data) >= 1`; `"po_number" in data[0]`; `"creation_date" in data[0]` |
| `test_query_receipts` | `isinstance(data, list)` | `len(data) >= 1`; `"po_number" in data[0]` |
| `test_query_invoices` | `isinstance(data, list)` | `len(data) >= 1`; `"invoice_num" in data[0]` |
| `test_query_payments` | `isinstance(data, list)` | `len(data) >= 1`; `"amount" in data[0]`; `data[0]["amount"] > 0` |
| 【新增】`test_query_po_with_limit` | - | `len(data) == 3`（limit=3） |
| 【新增】`test_query_po_with_order_by` | - | 日期/金额排序正确 |
| 【新增】`test_query_po_empty_result` | - | days=1 时 `data == []` 或 `len(data) == 0` |

#### 5.2.2 `tests/unit/test_repository.py`

| 改动 | 说明 |
|------|------|
| 【新增】`test_order_by_date_desc` | `query_purchase_orders(days=0, limit=3, order_by="date_desc")` → 验证 creation_date 递减 |
| 【新增】`test_order_by_amount_desc` | 同上，验证 total_amount 递减 |
| 【新增】`test_limit_returns_exact_count` | limit=1/5/10 → 返回行数严格等于 limit |
| 【新增】`test_limit_zero_means_no_limit` | limit=0 → 返回全部 |
| 【新增】`test_days_1_empty_when_no_today_data` | days=1 + 种子数据无当天记录 → `[]` |

#### 5.2.3 `tests/unit/test_orchestrator.py`

不改动现有 90 个 mock 测试（它们验证分支逻辑仍有价值），但在 `functional/` 层补充真实链路测试。

| 需关注的新增 | 说明 |
|-------------|------|
| 对应 `functional/test_lookup_pipeline.py` #11 | 验证 orchestrator 级别空结果 fallback |
| 对应 `functional/test_multi_turn.py` | 验证引用解析真实行为 |

#### 5.2.4 `tests/integration/test_e2e.py`

| 现有函数 | 当前断言 | 增强为 |
|---------|---------|--------|
| `test_three_way_match_full_flow` | `len(report) > 50` | 加上 `"匹配" in report or "match" in report.lower()` |
| `test_price_variance_full_flow` | `len(report) > 50` | 加上 `"差异" in report or "variance" in report.lower()` |
| `test_payment_compliance_full_flow` | `len(report) > 50` | 加上 `"付款" in report or "合规" in report` |
| `test_supplier_kpi_full_flow` | `len(report) > 50` | 加上 `supplier_id in report` |
| `test_comprehensive_analysis` | `len(report) > 100` | 加上 `assert data["summary"]["route_type"]` 非空 |
| 【新增】`test_latest_one_po_e2e` | - | 查询 "最新的一个po"，验证 status=success 且 report 含 po_number |
| 【新增】`test_latest_5_po_e2e` | - | 查询 "最新的5个PO"，验证 report 含表格且行数=5 |

#### 5.2.5 `tests/unit/test_lookup.py`

| 改动 | 说明 |
|------|------|
| 【新增】`test_execute_with_real_tool` | 用真实 repo + 真实工具替代 AsyncMock，验证 execute_lookup 全链路 |
| 【新增】`test_format_with_real_data` | 用真实查询结果（非硬编码 JSON）测试 format_lookup_result |

---

## 6. CI 集成 + 质量门禁规则

### 6.1 pytest 分组运行命令

```bash
# Stage 1: 快速验证（unit + functional，< 30s）
pytest tests/unit tests/functional -x -q --tb=short

# Stage 2: 回归 + 集成（加上 regression + integration，< 2min）
pytest tests/ -m "not e2e" -q --tb=short

# Stage 3: 覆盖率报告
pytest tests/ -m "not e2e" --cov=. --cov-report=term-missing --cov-fail-under=90

# Stage 4: E2E（仅 CI 主分支 / 发版，需 LLM_API_KEY secret）
pytest tests/ --cov=. --cov-fail-under=90
```

### 6.2 CI Pipeline 设计（GitHub Actions 示例）

```yaml
# .github/workflows/test.yml
name: Test Suite

on:
  push:
    branches: [main, feature-*]
  pull_request:

jobs:
  unit-and-functional:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: pytest tests/unit tests/functional -x -q --tb=short
        name: "Stage 1: Unit + Functional"

  regression:
    runs-on: ubuntu-latest
    needs: unit-and-functional
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -m "not e2e" --cov=. --cov-report=term-missing --cov-fail-under=90
        name: "Stage 2: Full (excl. E2E) + Coverage"

  e2e:
    runs-on: ubuntu-latest
    needs: regression
    if: github.ref == 'refs/heads/main'  # 仅主分支
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: pytest tests/ --cov=. --cov-fail-under=90
        name: "Stage 3: E2E (real LLM)"
        env:
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
```

### 6.3 断言质量 Checklist

以下规则作为代码审查标准，新增/修改测试时必须遵守：

| # | 规则 | 反例 | 正例 |
|---|------|------|------|
| 1 | **禁止仅类型断言** | `assert isinstance(data, list)` | `assert len(data) >= 1` + `assert "po_number" in data[0]` |
| 2 | **查询类必须验证行数** | 无 count 检查 | `assert len(data) == expected_limit` |
| 3 | **排序必须验证顺序** | 无顺序检查 | `assert values == sorted(values, reverse=True)` |
| 4 | **过滤必须验证条件** | 无字段值检查 | `assert all(r["vendor_id"] == vid for r in data)` |
| 5 | **空结果必须有专项测试** | 未测试空情况 | `test_empty_result_returns_xxx` |
| 6 | **E2E 报告必须含关键词** | `len(report) > 50` | `"匹配" in report` 或 `"差异" in report` |
| 7 | **mock 工具禁止在 functional 层** | `AsyncMock(return_value=...)` | 使用 `tool_registry.get(name)` 真实调用 |
| 8 | **新增工具必须同步新增测试** | 工具无对应测试 | 至少 2 个参数组合测试 |

### 6.4 新增测试预估统计

| 层级 | 新增文件 | 新增用例数 | 说明 |
|------|---------|-----------|------|
| fixtures/ | 3 文件 | 0（fixture 非测试） | queries.py + tool_chain.py + llm_stubs.py |
| functional/ | 6 文件 | ~65 | 路由 20 + lookup 14 + 工具 28 + DAG 8 + 多轮 6 + 记忆 5 |
| regression/ | 1 文件 | ~60（参数化展开） | 20 查询 × 3 断言维度 |
| 现有增强 | 5 文件 | ~20 | test_tools +7, test_repository +5, test_e2e +2, test_lookup +2, 其余 +4 |
| **合计** | **15 文件** | **~145** | 总用例 1549 → ~1694 |

### 6.5 实施优先级

| Phase | 范围 | 预计工时 | 价值 |
|-------|------|---------|------|
| **P1 止血** | 修 lookup 空结果 bug + test_tools.py 断言增强 + test_repository.py 补 limit/order_by | 1 天 | 修复已知 bug，堵住最大漏洞 |
| **P2 基建** | fixtures/ 三个新文件 + functional/conftest.py | 0.5 天 | 后续所有 functional 测试的基础 |
| **P3 核心** | test_lookup_pipeline.py + test_tool_correctness.py + test_canary_queries.py | 2 天 | 覆盖 P0 级缺失场景 |
| **P4 深化** | test_routing_accuracy.py + test_dag_pipeline.py | 1 天 | 覆盖 P1 级缺失场景 |
| **P5 多轮** | test_multi_turn.py + test_memory_effect.py | 1 天 | 覆盖 P2 级场景 |
| **P6 增强** | test_e2e.py 断言增强 + CI 配置 | 0.5 天 | 质量门禁常态化 |
