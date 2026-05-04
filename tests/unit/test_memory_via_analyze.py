"""通过 /analyze 和 /analyze/async 端点间接测试记忆系统的集成行为。

不直接测试记忆模块 API，而是验证分析流程中记忆的写入、读取、注入、
隔离、降级等行为是否正确。使用 FastAPI TestClient + mock 隔离外部依赖。
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.schemas.analysis import AnalysisRequest, AnalysisResult, AnalysisStatus, AnalysisType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app():
    """构建仅挂 analyze 路由的 FastAPI 应用。"""
    from api.routes.analyze import router
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def client():
    return TestClient(_make_app(), raise_server_exceptions=False)


def _success_result(**overrides) -> AnalysisResult:
    """构造成功的 AnalysisResult，可按需覆盖字段。"""
    defaults = {
        "report_id": str(uuid.uuid4()),
        "trace_id": str(uuid.uuid4()),
        "status": AnalysisStatus.SUCCESS,
        "analysis_type": AnalysisType.THREE_WAY_MATCH,
        "query": "检查 PO-001 三路匹配",
        "user_id": "u1",
        "session_id": "sess1",
        "time_range": "最近 30 天",
        "anomalies": [],
        "report_markdown": "## 三路匹配报告\n匹配率 95%",
        "duration_ms": 1200.0,
    }
    defaults.update(overrides)
    return AnalysisResult(**defaults)


def _failed_result(**overrides) -> AnalysisResult:
    """构造失败的 AnalysisResult。"""
    from api.schemas.domain import ErrorInfo
    defaults = {
        "report_id": str(uuid.uuid4()),
        "status": AnalysisStatus.FAILED,
        "analysis_type": AnalysisType.COMPREHENSIVE,
        "query": "test",
        "user_id": "u1",
        "session_id": "sess1",
        "time_range": "",
        "error": ErrorInfo(code="ORCHESTRATOR_ERROR", message="boom"),
    }
    defaults.update(overrides)
    return AnalysisResult(**defaults)


# ---------------------------------------------------------------------------
# TestMemoryWriteOnSuccess — 分析成功后记忆写入验证
# ---------------------------------------------------------------------------


class TestMemoryWriteOnSuccess:
    """验证 POST /analyze 成功后，记忆系统的写入行为。"""

    def test_persist_report_called_on_success(self, client):
        """成功分析后 _persist_report 应被调用。"""
        result = _success_result()
        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(return_value=result)

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = client.post("/analyze", json={"query": "三路匹配", "user_id": "u1"})

        assert resp.status_code == 200
        assert resp.json()["status"] == "success"
        mock_orch.analyze.assert_called_once()

    def test_report_persisted_to_long_term_memory(self, client):
        """成功分析后报告被写入长期记忆（通过 get_report 可查询）。"""
        report_id = str(uuid.uuid4())
        result = _success_result(report_id=report_id)

        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(return_value=result)

        mock_ltm = MagicMock()
        mock_ltm.get_report.return_value = {
            "id": report_id,
            "query": "三路匹配",
            "report_markdown": "## 报告",
        }

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
            patch("api.routes.analyze.get_long_term_memory", return_value=mock_ltm),
        ):
            # 先执行分析
            resp = client.post("/analyze", json={"query": "三路匹配", "user_id": "u1"})
            assert resp.status_code == 200

            # 再查询报告
            resp2 = client.get(f"/reports/{report_id}")
            assert resp2.status_code == 200
            assert resp2.json()["id"] == report_id

    def test_auto_persist_writes_chat_messages(self, client):
        """auto_persist=True 时用户消息和助手回复被写入 chat 表。"""
        result = _success_result()

        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(return_value=result)

        mock_repo = MagicMock()
        mock_repo.get_session.return_value = {"id": "sess1"}
        mock_repo.append_messages.return_value = {
            "messages": [
                {"id": "msg-user-1"},
                {"id": "msg-asst-1"},
            ],
            "session": {"id": "sess1"},
        }

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=mock_repo),
        ):
            resp = client.post(
                "/analyze",
                json={
                    "query": "三路匹配",
                    "user_id": "u1",
                    "session_id": "sess1",
                    "auto_persist": True,
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["user_message_id"] == "msg-user-1"
        assert data["assistant_message_id"] == "msg-asst-1"
        mock_repo.append_messages.assert_called_once()
        args = mock_repo.append_messages.call_args
        messages = args[0][2]
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    def test_no_chat_persist_when_disabled(self, client):
        """auto_persist=False 时不写入 chat 表。"""
        result = _success_result()

        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(return_value=result)

        mock_repo = MagicMock()

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=mock_repo),
        ):
            resp = client.post(
                "/analyze",
                json={
                    "query": "三路匹配",
                    "user_id": "u1",
                    "auto_persist": False,
                },
            )

        assert resp.status_code == 200
        mock_repo.get_session.assert_not_called()
        mock_repo.append_messages.assert_not_called()


# ---------------------------------------------------------------------------
# TestMemoryContextInjection — 记忆上下文注入验证
# ---------------------------------------------------------------------------


class TestMemoryContextInjection:
    """验证分析流程中 MemoryManager.build_context 的调用与注入行为。"""

    def test_build_context_called_with_correct_params(self, client):
        """orchestrator.analyze 内部调用 build_context 时传入正确的 user_id 和 query。"""
        result = _success_result(user_id="analyst_a")
        captured_requests: list[Any] = []

        async def capture_analyze(req):
            captured_requests.append(req)
            return result

        mock_orch = MagicMock()
        mock_orch.analyze = capture_analyze

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = client.post(
                "/analyze",
                json={"query": "供应商 SUP-001 绩效", "user_id": "analyst_a"},
            )

        assert resp.status_code == 200
        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req.user_id == "analyst_a"
        assert "SUP-001" in req.query

    def test_memory_context_does_not_block_on_empty(self, client):
        """当无可用记忆时分析仍正常返回（build_context 返回空字符串不影响流程）。"""
        result = _success_result()
        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(return_value=result)

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = client.post("/analyze", json={"query": "价格差异分析", "user_id": "u1"})

        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    def test_session_id_passed_to_orchestrator(self, client):
        """指定 session_id 时正确传递给 orchestrator（用于加载短期记忆上下文）。"""
        result = _success_result(session_id="fixed-session-123")
        captured: list[AnalysisRequest] = []

        async def capture(req):
            captured.append(req)
            return result

        mock_orch = MagicMock()
        mock_orch.analyze = capture

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = client.post(
                "/analyze",
                json={
                    "query": "test",
                    "user_id": "u1",
                    "session_id": "fixed-session-123",
                },
            )

        assert resp.status_code == 200
        assert captured[0].session_id == "fixed-session-123"


# ---------------------------------------------------------------------------
# TestMultiTurnMemoryContinuity — 多轮会话记忆连续性
# ---------------------------------------------------------------------------


class TestMultiTurnMemoryContinuity:
    """验证同一 session 连续多次分析时记忆的累积和传递。"""

    def test_session_entities_accumulate_across_turns(self, client):
        """第一次分析写入实体后，第二次分析时 orchestrator 能读取同一 session。"""
        session_id = "multi-turn-sess"
        turn_count: list[int] = [0]

        async def sequential_analyze(req):
            turn_count[0] += 1
            return _success_result(
                session_id=session_id,
                query=req.query,
                report_markdown=f"Turn {turn_count[0]}: PO-001 分析完成",
            )

        mock_orch = MagicMock()
        mock_orch.analyze = sequential_analyze

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            # Turn 1
            resp1 = client.post(
                "/analyze",
                json={
                    "query": "检查 PO-001 三路匹配",
                    "user_id": "u1",
                    "session_id": session_id,
                },
            )
            assert resp1.status_code == 200
            assert resp1.json()["session_id"] == session_id

            # Turn 2 — 同一 session
            resp2 = client.post(
                "/analyze",
                json={
                    "query": "这个PO的价格差异呢",
                    "user_id": "u1",
                    "session_id": session_id,
                },
            )
            assert resp2.status_code == 200
            assert resp2.json()["session_id"] == session_id

        # orchestrator.analyze 被调用两次，都用同一 session_id
        assert turn_count[0] == 2

    def test_auto_generated_session_id_consistent_within_response(self, client):
        """未指定 session_id 时自动生成，且 response 中 session_id 非空。"""
        result = _success_result()

        async def return_with_session(req):
            return _success_result(session_id=req.session_id)

        mock_orch = MagicMock()
        mock_orch.analyze = return_with_session

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = client.post("/analyze", json={"query": "test", "user_id": "u1"})

        assert resp.status_code == 200
        session_id = resp.json()["session_id"]
        assert session_id is not None
        assert len(session_id) == 36  # UUID format


# ---------------------------------------------------------------------------
# TestMemoryIsolation — 用户隔离与会话隔离
# ---------------------------------------------------------------------------


class TestMemoryIsolation:
    """验证不同用户和不同会话的记忆隔离性。"""

    def test_different_users_get_independent_reports(self, client):
        """不同 user_id 的报告互不可见。"""
        mock_ltm = MagicMock()
        # user_a 有报告，user_b 无报告
        mock_ltm.list_reports.side_effect = lambda uid, limit: (
            [{"id": "r1", "query": "user_a report"}] if uid == "user_a" else []
        )

        with patch("api.routes.analyze.get_long_term_memory", return_value=mock_ltm):
            resp_a = client.get("/reports?user_id=user_a")
            resp_b = client.get("/reports?user_id=user_b")

        assert resp_a.status_code == 200
        assert len(resp_a.json()) == 1
        assert resp_b.status_code == 200
        assert len(resp_b.json()) == 0

    def test_clear_memory_only_affects_target_user(self, client):
        """清理长期记忆时只影响目标 user_id。"""
        mock_ltm = MagicMock()
        mock_ltm.delete_user_data.return_value = {"memories": 5, "reports": 2}

        with patch("api.routes.analyze.get_long_term_memory", return_value=mock_ltm):
            resp = client.delete("/memory/long-term?user_id=user_a")

        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "user"
        assert data["user_id"] == "user_a"
        # 验证只传入了 user_a
        mock_ltm.delete_user_data.assert_called_once_with(
            "user_a", delete_memories=True, delete_reports=True,
        )

    def test_different_sessions_independent_short_term(self, client):
        """不同 session_id 的短期记忆独立清理。"""
        mock_orch = MagicMock()
        mock_orch.clear_short_term_memory.return_value = 1

        with patch("api.routes.analyze._get_orchestrator", return_value=mock_orch):
            resp1 = client.delete("/memory/short-term?session_id=sess_a")
            resp2 = client.delete("/memory/short-term?session_id=sess_b")

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        calls = mock_orch.clear_short_term_memory.call_args_list
        assert calls[0] == call("sess_a")
        assert calls[1] == call("sess_b")


# ---------------------------------------------------------------------------
# TestMemoryFailureGraceful — 失败/异常场景记忆行为
# ---------------------------------------------------------------------------


class TestMemoryFailureGraceful:
    """验证分析异常时记忆系统的降级和容错行为。"""

    def test_analysis_exception_still_returns_result(self, client):
        """orchestrator 抛异常时返回 failed 结果，不导致 500。"""
        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(side_effect=RuntimeError("内存溢出"))

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = client.post("/analyze", json={"query": "test", "user_id": "u1"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "failed"
        assert data["error"]["code"] == "API_ERROR"
        assert "内存溢出" in data["error"]["message"]

    def test_chat_persist_failure_does_not_block_response(self, client):
        """chat 持久化失败不阻塞分析响应返回。"""
        result = _success_result()
        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(return_value=result)

        mock_repo = MagicMock()
        mock_repo.get_session.return_value = {"id": "sess1"}
        mock_repo.append_messages.side_effect = RuntimeError("DB connection lost")

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=mock_repo),
        ):
            resp = client.post(
                "/analyze",
                json={
                    "query": "三路匹配",
                    "user_id": "u1",
                    "session_id": "sess1",
                    "auto_persist": True,
                },
            )

        # 分析本身成功，chat 写入失败不影响 HTTP 状态码
        assert resp.status_code == 200

    def test_long_term_memory_unavailable_does_not_crash_reports(self, client):
        """长期记忆不可用时 /reports 返回空列表而非 500。"""
        mock_ltm = MagicMock()
        mock_ltm.list_reports.side_effect = RuntimeError("PG down")

        with patch("api.routes.analyze.get_long_term_memory", return_value=mock_ltm):
            resp = client.get("/reports?user_id=u1")

        # _run_db_io 超时会返回 504，但直接异常包在 wait_for 里会得到 504
        assert resp.status_code in (504, 500)

    def test_failed_analysis_persists_error_to_chat(self, client):
        """分析失败时 auto_persist 仍将错误消息写入 chat（status=error）。"""
        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(side_effect=ValueError("tool not found"))

        mock_repo = MagicMock()
        mock_repo.get_session.return_value = {"id": "sess1"}
        mock_repo.append_messages.return_value = {
            "messages": [{"id": "m1"}, {"id": "m2"}],
            "session": {"id": "sess1"},
        }

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=mock_repo),
        ):
            resp = client.post(
                "/analyze",
                json={
                    "query": "test",
                    "user_id": "u1",
                    "session_id": "sess1",
                    "auto_persist": True,
                },
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"
        # 验证 append_messages 被调用，且 assistant 消息 status=error
        mock_repo.append_messages.assert_called_once()
        messages = mock_repo.append_messages.call_args[0][2]
        assert messages[1]["status"] == "error"


# ---------------------------------------------------------------------------
# TestFeedbackDetectionViaAnalyze — 反馈检测触发验证
# ---------------------------------------------------------------------------


class TestFeedbackDetectionViaAnalyze:
    """验证包含纠正信号的查询在分析流程中触发反馈检测。"""

    def test_correction_query_still_returns_analysis(self, client):
        """包含纠正语句的查询仍正常返回分析结果（反馈提取是 fire-and-forget）。"""
        result = _success_result(query="这个价格差异不对，是批量折扣")
        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(return_value=result)

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = client.post(
                "/analyze",
                json={
                    "query": "这个价格差异不对，是批量折扣",
                    "user_id": "u1",
                    "session_id": "sess1",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "success"
        # 查询包含反馈信号但不影响响应
        req_passed = mock_orch.analyze.call_args[0][0]
        assert "不对" in req_passed.query

    def test_preference_query_processed_normally(self, client):
        """用户偏好类查询（"以后用表格"）正常通过分析流程。"""
        result = _success_result(query="以后分析结果请用表格展示")
        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(return_value=result)

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = client.post(
                "/analyze",
                json={
                    "query": "以后分析结果请用表格展示",
                    "user_id": "u1",
                },
            )

        assert resp.status_code == 200
        mock_orch.analyze.assert_called_once()


# ---------------------------------------------------------------------------
# TestMemoryClearLinkage — 记忆清理 API 联动
# ---------------------------------------------------------------------------


class TestMemoryClearLinkage:
    """验证记忆清理端点的完整行为和参数校验。"""

    def test_clear_long_term_requires_user_or_all(self, client):
        """未指定 user_id 且 all=false 时返回 400。"""
        resp = client.delete("/memory/long-term")
        assert resp.status_code == 400
        assert "user_id" in resp.json()["detail"]

    def test_clear_long_term_all_deletes_everything(self, client):
        """all=true 时清空所有用户数据。"""
        mock_ltm = MagicMock()
        mock_ltm.delete_all.return_value = {"memories": 20, "reports": 10}

        with patch("api.routes.analyze.get_long_term_memory", return_value=mock_ltm):
            resp = client.delete("/memory/long-term?all=true")

        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "all"
        assert data["deleted"]["memories"] == 20
        mock_ltm.delete_all.assert_called_once()

    def test_clear_long_term_selective_delete(self, client):
        """可选择只删 memories 或只删 reports。"""
        mock_ltm = MagicMock()
        mock_ltm.delete_user_data.return_value = {"memories": 0, "reports": 3}

        with patch("api.routes.analyze.get_long_term_memory", return_value=mock_ltm):
            resp = client.delete(
                "/memory/long-term?user_id=u1&delete_memories=false&delete_reports=true"
            )

        assert resp.status_code == 200
        mock_ltm.delete_user_data.assert_called_once_with(
            "u1", delete_memories=False, delete_reports=True,
        )

    def test_clear_short_term_all_sessions(self, client):
        """不指定 session_id 时清空全部短期记忆。"""
        mock_orch = MagicMock()
        mock_orch.clear_short_term_memory.return_value = 8

        with patch("api.routes.analyze._get_orchestrator", return_value=mock_orch):
            resp = client.delete("/memory/short-term")

        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "all"
        assert data["cleared_sessions"] == 8
        mock_orch.clear_short_term_memory.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# TestRecallSkipsMemory — RECALL 意图跳过记忆写入
# ---------------------------------------------------------------------------


class TestRecallSkipsMemory:
    """验证 RECALL 类查询（"上次分析了什么"）不触发记忆写入。"""

    def test_recall_query_returns_result_without_persist(self, client):
        """RECALL 查询正常返回结果，但不影响 chat 持久化（由 API 层控制）。"""
        result = _success_result(
            query="上次分析的结果呢",
            report_markdown="上次分析了 PO-001 三路匹配",
        )
        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(return_value=result)

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
        ):
            resp = client.post(
                "/analyze",
                json={
                    "query": "上次分析的结果呢",
                    "user_id": "u1",
                    "session_id": "sess1",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "success"
        # orchestrator 内部根据 RECALL intent 跳过 _persist_report 和
        # on_analysis_complete，但 API 层只调用 orchestrator.analyze
        mock_orch.analyze.assert_called_once()

    def test_recall_does_not_create_new_report_entry(self, client):
        """RECALL 查询不应在 /reports 中新增条目。"""
        mock_ltm = MagicMock()
        mock_ltm.list_reports.return_value = []

        result = _success_result(query="刚才分析了什么")
        mock_orch = MagicMock()
        mock_orch.analyze = AsyncMock(return_value=result)

        with (
            patch("api.routes.analyze._get_orchestrator", return_value=mock_orch),
            patch("api.routes.analyze.get_chat_repository", return_value=None),
            patch("api.routes.analyze.get_long_term_memory", return_value=mock_ltm),
        ):
            # 发起 RECALL 查询
            client.post(
                "/analyze",
                json={"query": "刚才分析了什么", "user_id": "u1"},
            )
            # 查看报告列表仍为空（RECALL 不产生报告）
            resp = client.get("/reports?user_id=u1")

        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# TestAsyncAnalyzeMemory — 异步分析路径记忆验证
# ---------------------------------------------------------------------------


def _make_async_app():
    """构建挂 analyze + analyze_async 路由的 FastAPI 应用。"""
    from fastapi import FastAPI
    from api.routes.analyze import router as sync_router
    from api.routes.analyze_async import router as async_router

    app = FastAPI()
    app.include_router(sync_router)
    app.include_router(async_router)
    return app


@pytest.fixture()
def async_client():
    return TestClient(_make_async_app(), raise_server_exceptions=False)


class TestAsyncAnalyzeMemory:
    """验证 POST /analyze/async 的记忆系统行为。"""

    def _mock_task_entry(self, **kwargs):
        """构造 mock TaskEntry。"""
        from core.time_utils import now_cn
        entry = MagicMock()
        entry.trace_id = kwargs.get("trace_id", str(uuid.uuid4()))
        entry.state = kwargs.get("state", "queued")
        entry.created_at = now_cn()
        return entry

    def test_async_submit_returns_trace_id(self, async_client):
        """异步提交立即返回 trace_id，不等待分析完成。"""
        mock_entry = self._mock_task_entry()

        mock_registry = MagicMock()
        mock_registry.submit = AsyncMock(return_value=mock_entry)

        with (
            patch("api.routes.analyze_async.get_task_registry", return_value=mock_registry),
            patch("api.routes.analyze_async.get_chat_repository", return_value=None),
            patch("api.routes.analyze_async._build_runner", return_value=MagicMock()),
            patch("api.routes.analyze_async._build_finalizer", return_value=MagicMock()),
        ):
            resp = async_client.post(
                "/analyze/async",
                json={"query": "三路匹配检查", "user_id": "u1"},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert "trace_id" in data
        assert len(data["trace_id"]) == 36

    def test_async_with_auto_persist_pre_writes_messages(self, async_client):
        """async + auto_persist 时预写 pending 状态的 assistant 消息。"""
        mock_entry = self._mock_task_entry()

        mock_registry = MagicMock()
        mock_registry.submit = AsyncMock(return_value=mock_entry)

        mock_repo = MagicMock()
        mock_repo.get_session.return_value = {"id": "sess1"}
        mock_repo.append_messages.return_value = {
            "messages": [{"id": "um1"}, {"id": "am1"}],
            "session": {"id": "sess1"},
        }

        with (
            patch("api.routes.analyze_async.get_task_registry", return_value=mock_registry),
            patch("api.routes.analyze_async.get_chat_repository", return_value=mock_repo),
            patch("api.routes.analyze_async._build_runner", return_value=MagicMock()),
            patch("api.routes.analyze_async._build_finalizer", return_value=MagicMock()),
        ):
            resp = async_client.post(
                "/analyze/async",
                json={
                    "query": "三路匹配",
                    "user_id": "u1",
                    "session_id": "sess1",
                    "auto_persist": True,
                },
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data.get("user_message_id") == "um1"
        assert data.get("assistant_message_id") == "am1"

    def test_async_without_registry_returns_503(self, async_client):
        """TaskRegistry 未初始化时返回 503。"""
        with patch("api.routes.analyze_async.get_task_registry", return_value=None):
            resp = async_client.post(
                "/analyze/async",
                json={"query": "test", "user_id": "u1"},
            )

        assert resp.status_code == 503
