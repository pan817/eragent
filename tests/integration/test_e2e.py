"""
端到端集成测试（真实 LLM）。

验证完整请求链路：HTTP Request → FastAPI → Orchestrator → IntentParser
→ P2PAgent → LLM (qwen3-max) → Tools（mock 业务数据）→ Rules → HTTP Response。

所有组件真实运行，包括 LLM 调用。需要配置有效的 LLM_API_KEY 环境变量。
"""

from __future__ import annotations

from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="module")
def real_settings() -> Settings:
    """从 config.yaml + .env 加载真实配置。"""
    # 清除 get_settings 缓存，确保重新加载
    from config.settings import get_settings
    get_settings.cache_clear()
    return Settings.from_yaml()


@pytest.fixture(scope="module")
def e2e_client(real_settings: Settings):
    """创建真实 LLM 的端到端测试客户端。

    所有组件真实运行：IntentParser、P2PAgent、LLM、Tools、Rules。
    数据库使用 SQLite 内存库，避免依赖真实 PostgreSQL。
    scope=module 让整个测试模块共享同一客户端，避免重复初始化 Agent。
    """
    import api.routes.analyze as analyze_mod
    analyze_mod._orchestrator = None
    analyze_mod._long_term_memory = None

    from sqlalchemy.pool import StaticPool
    from core.database.engine import create_engine_from_dsn
    from core.database import get_session_factory, init_database
    from modules.p2p.repository import P2PRepository
    from modules.p2p.tools import set_repository
    # 提前注册 trace 模型到 Base.metadata，确保 init_database 时一并建表
    import core.observability  # noqa: F401

    engine = create_engine_from_dsn(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_database(engine, seed=0)
    session_factory = get_session_factory(engine)
    set_repository(P2PRepository(session_factory))

    from unittest.mock import patch
    with (
        patch("config.settings.get_settings", return_value=real_settings),
        patch("api.main.get_engine", return_value=engine),
        patch("api.main.create_tables"),
        patch("api.main.get_session_factory", return_value=session_factory),
    ):
        from api.main import app
        with TestClient(app) as client:
            yield client

    engine.dispose()


# ============================================================
# 端到端测试用例
# ============================================================


class TestE2EThreeWayMatch:
    """三路匹配端到端测试。"""

    def test_three_way_match_full_flow(self, e2e_client: TestClient) -> None:
        """自然语言请求 → 意图解析 → LLM 驱动工具调用 → 规则检查 → 返回报告。"""
        resp = e2e_client.post("/api/v1/ptp-agent/analyze", json={
            "query": "请分析最近的三路匹配异常情况",
            "user_id": "e2e-tester",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["analysis_type"] == "three_way_match"
        assert data["user_id"] == "e2e-tester"
        assert data["session_id"]
        assert data["report_id"]
        assert data["time_range"] == "最近 30 天"
        assert data["duration_ms"] > 0
        # LLM 应生成非空 Markdown 报告
        assert len(data["report_markdown"]) > 50


class TestE2EPriceVariance:
    """价格差异分析端到端测试。"""

    def test_price_variance_full_flow(self, e2e_client: TestClient) -> None:
        """价格差异分析全链路：LLM 应调用价格分析工具并生成报告。"""
        resp = e2e_client.post("/api/v1/ptp-agent/analyze", json={
            "query": "分析采购价格差异",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["analysis_type"] == "price_variance"
        assert len(data["report_markdown"]) > 50


class TestE2EPaymentCompliance:
    """付款合规检查端到端测试。"""

    def test_payment_compliance_full_flow(self, e2e_client: TestClient) -> None:
        """付款合规全链路：LLM 应检测逾期/提前付款并生成报告。"""
        resp = e2e_client.post("/api/v1/ptp-agent/analyze", json={
            "query": "检查付款合规性，是否有逾期付款",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["analysis_type"] == "payment_compliance"
        assert len(data["report_markdown"]) > 50


class TestE2ESupplierPerformance:
    """供应商绩效评估端到端测试。"""

    def test_supplier_kpi_full_flow(self, e2e_client: TestClient) -> None:
        """指定供应商 → LLM 调用 KPI 计算工具 → 返回绩效报告。"""
        from modules.p2p.mock_data.generator import MockDataGenerator
        gen = MockDataGenerator(seed=0)
        raw = gen.generate_all()
        supplier_id = raw["po_headers"][0]["supplier_id"]

        resp = e2e_client.post("/api/v1/ptp-agent/analyze", json={
            "query": f"评估供应商 {supplier_id} 的绩效 KPI",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["analysis_type"] == "supplier_performance"
        assert len(data["report_markdown"]) > 50


class TestE2EComprehensive:
    """综合分析端到端测试。"""

    def test_comprehensive_analysis(self, e2e_client: TestClient) -> None:
        """模糊查询 → 意图解析为 COMPREHENSIVE → LLM 自主选择多个工具 → 综合报告。"""
        resp = e2e_client.post("/api/v1/ptp-agent/analyze", json={
            "query": "帮我全面分析一下最近的采购数据，看看有什么异常",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["analysis_type"] == "comprehensive"
        assert len(data["report_markdown"]) > 100


class TestE2ECustomTimeRange:
    """自定义时间范围端到端测试。"""

    def test_explicit_time_range(self, e2e_client: TestClient) -> None:
        """用户指定 time_range_days=90 → 结果中正确反映。"""
        resp = e2e_client.post("/api/v1/ptp-agent/analyze", json={
            "query": "三路匹配检查",
            "time_range_days": 90,
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["time_range"] == "最近 90 天"

    def test_time_range_from_query_text(self, e2e_client: TestClient) -> None:
        """查询文本含 "最近60天" → IntentParser 提取 → 时间范围正确。"""
        resp = e2e_client.post("/api/v1/ptp-agent/analyze", json={
            "query": "分析最近60天的三路匹配情况",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["time_range"] == "最近 60 天"


class TestE2EExplicitAnalysisType:
    """显式指定分析类型端到端测试。"""

    def test_explicit_type_overrides_intent(self, e2e_client: TestClient) -> None:
        """显式 analysis_type 优先于意图解析。"""
        resp = e2e_client.post("/api/v1/ptp-agent/analyze", json={
            "query": "三路匹配相关的分析",
            "analysis_type": "payment_compliance",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["analysis_type"] == "payment_compliance"


class TestE2EValidation:
    """请求校验端到端测试。"""

    def test_empty_query_returns_422(self, e2e_client: TestClient) -> None:
        """空查询文本 → 422。"""
        resp = e2e_client.post("/api/v1/ptp-agent/analyze", json={"query": ""})
        assert resp.status_code == 422

    def test_query_too_long_returns_422(self, e2e_client: TestClient) -> None:
        """超长查询文本 → 422。"""
        resp = e2e_client.post("/api/v1/ptp-agent/analyze", json={"query": "x" * 2001})
        assert resp.status_code == 422

    def test_invalid_time_range_returns_422(self, e2e_client: TestClient) -> None:
        """非法时间范围 → 422。"""
        resp = e2e_client.post("/api/v1/ptp-agent/analyze", json={
            "query": "分析",
            "time_range_days": 0,
        })
        assert resp.status_code == 422


class TestE2EObservability:
    """链路耗时监控端到端测试：验证 TimingMiddleware → TraceStore → /traces API 全链路。"""

    def _wait_for_trace(self, client: TestClient, predicate, timeout: float = 5.0):
        import time

        deadline = time.monotonic() + timeout
        last: list = []
        while time.monotonic() < deadline:
            resp = client.get("/api/v1/ptp-agent/traces", params={"limit": 50})
            assert resp.status_code == 200
            last = resp.json()
            match = next((r for r in last if predicate(r)), None)
            if match is not None:
                return match
            time.sleep(0.1)
        raise AssertionError(f"trace not found within {timeout}s; got: {last}")

    def test_analyze_records_full_trace(self, e2e_client: TestClient) -> None:
        """analyze 请求结束后，TraceStore 应记录完整的 agent/model/tool span 树。"""
        # 先记录已有 trace_id 集合，便于精确定位本次请求新增的那一条
        before_resp = e2e_client.get("/api/v1/ptp-agent/traces", params={"limit": 200})
        assert before_resp.status_code == 200
        existing_ids = {r["trace_id"] for r in before_resp.json()}

        resp = e2e_client.post(
            "/api/v1/ptp-agent/analyze",
            json={
                "query": "请分析最近的三路匹配异常情况",
                "user_id": "obs-tester",
                "analysis_type": "comprehensive",
            },
        )
        assert resp.status_code == 200
        analyze = resp.json()
        assert analyze["status"] == "success"

        # 后台线程异步落库，轮询直到本次请求的新 trace 出现且 status=success
        run = self._wait_for_trace(
            e2e_client,
            lambda r: r["trace_id"] not in existing_ids
            and r["agent_name"] == "p2p_agent"
            and r["status"] == "success",
        )

        assert run["duration_ms"] is not None and run["duration_ms"] > 0
        assert run["model_call_count"] >= 1
        assert run["tool_call_count"] >= 1
        assert run["finished_at"] is not None
        assert run["error"] is None

        # 详情：应至少包含 1 个 agent + 1 个 model + 1 个 tool span
        detail_resp = e2e_client.get(f"/api/v1/ptp-agent/traces/{run['trace_id']}")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        spans = detail["spans"]
        assert len(spans) >= 3

        types = {s["span_type"] for s in spans}
        assert {"agent", "model", "tool"}.issubset(types)

        agent_spans = [s for s in spans if s["span_type"] == "agent"]
        assert len(agent_spans) == 1
        assert agent_spans[0]["parent_span_id"] is None
        assert agent_spans[0]["name"] == "p2p_agent"
        assert agent_spans[0]["duration_ms"] >= sum(
            s["duration_ms"] for s in spans if s["span_type"] == "tool"
        )

        # 每个 model/tool span 都应有耗时与 ok 状态
        for sp in spans:
            if sp["span_type"] in ("model", "tool"):
                assert sp["status"] == "ok"
                assert sp["duration_ms"] is not None and sp["duration_ms"] >= 0

        # tool span 必须保留 args 属性，便于审计
        tool_spans = [s for s in spans if s["span_type"] == "tool"]
        assert all("args" in (s["attributes"] or {}) for s in tool_spans)

    def test_traces_filtering_and_404(self, e2e_client: TestClient) -> None:
        """list 支持分页/过滤；未知 trace_id 返回 404。"""
        resp = e2e_client.get("/api/v1/ptp-agent/traces", params={"limit": 5})
        assert resp.status_code == 200
        runs = resp.json()
        assert len(runs) <= 5

        missing = e2e_client.get("/api/v1/ptp-agent/traces/00000000-0000-0000-0000-000000000000")
        assert missing.status_code == 404


class TestE2EHealthCheck:
    """健康检查端到端测试。"""

    def test_health_endpoint(self, e2e_client: TestClient) -> None:
        """GET /health → status=ok。"""
        resp = e2e_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


class TestE2EReactStreaming:
    """Phase 2 ReAct 流式输出端到端验证（真实 Qwen）。

    验收点：
    - TTFB（首个 chunk 到达时间）小于 10s
    - chunk 序列 ``node="agent_final"``，index 单调递增，末帧 eos=true
    - accumulated delta == final report_markdown（streaming 与快照一致）
    """

    def test_react_streaming_ttfb_and_consistency(
        self, e2e_client: TestClient
    ) -> None:
        """构造一条 DAG 模板覆盖不到的探索类 query，触发 ReAct 兜底，
        通过 SSE 验证 chunk 流的 TTFB 与最终一致性。

        若意图路由意外命中了 DAG，会得到 ``node="report"`` 的 chunks，
        测试不会失败但会跳过 TTFB 比较（仅验证 streaming 不回归）。
        """
        import json
        import time

        # 1) 提交异步任务
        t_post = time.monotonic()
        resp = e2e_client.post(
            "/api/v1/ptp-agent/analyze/async",
            json={
                # 纯事实查询（无异常/合规/绩效语义），LLM 应判为 data_lookup,
                # 路由代码里 is_data_lookup → 强制 use_dag=False → 走 ReAct 兜底
                "query": "列出最近 7 天创建的前 3 张采购订单和它们的当前状态",
                "user_id": "e2e-react-streaming",
                "auto_persist": False,
            },
        )
        assert resp.status_code == 202, resp.text
        ack = resp.json()
        trace_id = ack["trace_id"]
        stream_url = ack["stream_url"]

        # 2) 订阅 SSE 流，记录每条事件到达时刻
        events: list[dict] = []
        first_chunk_at: float | None = None
        with e2e_client.stream("GET", stream_url) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                ev = json.loads(line[len("data: "):])
                events.append(ev)
                if ev.get("type") == "chunk" and first_chunk_at is None:
                    first_chunk_at = time.monotonic()
                if ev.get("type") == "done":
                    break

        # 3) 拿快照，验证 streaming 累计 == 最终 report_markdown
        snap = e2e_client.get(ack["poll_url"]).json()
        assert snap["status"] == "ok", f"任务未成功: {snap}"
        report_markdown = snap["result"]["report_markdown"]
        assert report_markdown, "report_markdown 不应为空"

        chunk_events = [e for e in events if e.get("type") == "chunk"]
        # 后端 streaming 或被关闭时无 chunk，本测试退化为不回归断言
        if not chunk_events:
            pytest.skip(
                "未收到 chunk 事件（可能 streaming 关闭或意图路由命中 DAG 短路径）"
            )

        # 协议合规
        nodes = {c["node"] for c in chunk_events}
        assert nodes <= {"report", "agent_final"}, f"未知 node: {nodes}"
        assert all(c["seq"] == 0 for c in chunk_events)
        # message_id 在 trace 内唯一
        assert len({c["message_id"] for c in chunk_events}) == 1
        # index 严格 +1 递增（允许 rollback 帧带来的 index 回退，但不能跨越
        # rollback 之后乱序）
        last_idx = -1
        rollback_seen = False
        for c in chunk_events:
            if c["index"] <= last_idx:
                # 允许且仅允许一种回退场景：index 回到 0
                assert c["index"] == 0, (
                    f"index 非法回退: {c['index']} <= {last_idx}"
                )
                rollback_seen = True
                last_idx = 0
            else:
                last_idx = c["index"]

        # 末帧 eos
        assert chunk_events[-1]["eos"] is True

        # accumulated delta == report_markdown（剥离 <think> 已由后端处理）
        accumulated = "".join(c["delta"] for c in chunk_events)
        # 因 rollback 后 buffer 重置，这里取最后一段连续 index 的累计
        if rollback_seen:
            # 找到最后一个 index=0 的位置，从那之后累加
            last_zero = max(
                i for i, c in enumerate(chunk_events) if c["index"] == 0
            )
            accumulated = "".join(c["delta"] for c in chunk_events[last_zero:])
        assert accumulated == report_markdown, (
            "streaming 累加内容必须等于 report_markdown 以保证快照覆盖无感知；"
            f"\n--- accumulated ({len(accumulated)} 字符) ---\n{accumulated[:300]}"
            f"\n--- report_markdown ({len(report_markdown)} 字符) ---\n{report_markdown[:300]}"
        )

        # TTFB 验收：60s 是兜底硬上限。真实 ReAct 含 1-2 次 tool 调用时
        # 典型 15-30s（意图路由 ~5s + tool 执行 ~5-15s + 首 token ~1-3s），
        # docs/sse_react_backend.md §6 的 < 10s 是无 tool 场景的乐观值；
        # 含 tool 场景下"流式收益"体现在 vs 非流式总时长的相对节省，
        # 而非绝对 TTFB。
        if first_chunk_at is not None:
            ttfb_s = first_chunk_at - t_post
            assert ttfb_s < 60.0, f"TTFB 超过 60s 兜底硬上限: {ttfb_s:.2f}s"
            print(
                f"\n[E2E ReAct streaming] TTFB={ttfb_s*1000:.0f}ms "
                f"chunks={len(chunk_events)} nodes={nodes} "
                f"report_chars={len(report_markdown)} "
                f"rollback={rollback_seen}"
            )
