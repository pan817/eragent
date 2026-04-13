"""api/routes/sessions.py 路由单元测试。

使用 FastAPI TestClient + mock ChatRepository，不需要真实 DB。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.sessions import router, init_chat_repo


def _make_app(mock_repo: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/ptp-agent")
    init_chat_repo(mock_repo)
    return app


def _session_dict(**overrides) -> dict:
    base = {
        "id": "s-001",
        "user_id": "alice",
        "title": "新对话",
        "title_auto": True,
        "message_count": 0,
        "last_message_preview": None,
        "created_at": "2026-04-11T08:00:00+00:00",
        "updated_at": "2026-04-11T08:00:00+00:00",
    }
    base.update(overrides)
    return base


def _message_dict(**overrides) -> dict:
    base = {
        "id": "m-001",
        "client_id": None,
        "session_id": "s-001",
        "role": "user",
        "content": "hello",
        "status": "success",
        "duration_ms": None,
        "trace_id": None,
        "created_at": "2026-04-11T08:00:00+00:00",
        "metadata": None,
    }
    base.update(overrides)
    return base


HEADERS = {"X-User-Id": "alice"}


@pytest.fixture()
def mock_repo():
    return MagicMock()


@pytest.fixture()
def client(mock_repo):
    app = _make_app(mock_repo)
    return TestClient(app)


# ============================================================
# 4.1 列出会话
# ============================================================


class TestListSessions:
    def test_ok(self, client, mock_repo) -> None:
        mock_repo.list_sessions.return_value = {
            "sessions": [_session_dict()],
            "next_cursor": None,
            "total": 1,
        }
        resp = client.get("/api/v1/ptp-agent/sessions", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["sessions"]) == 1

    def test_unauthorized(self, client) -> None:
        resp = client.get("/api/v1/ptp-agent/sessions")
        assert resp.status_code == 401


# ============================================================
# 4.2 创建会话
# ============================================================


class TestCreateSession:
    def test_ok(self, client, mock_repo) -> None:
        mock_repo.create_session.return_value = _session_dict()
        resp = client.post("/api/v1/ptp-agent/sessions", headers=HEADERS, json={"title": "新对话"})
        assert resp.status_code == 201
        assert resp.json()["session"]["id"] == "s-001"

    def test_no_body(self, client, mock_repo) -> None:
        mock_repo.create_session.return_value = _session_dict()
        resp = client.post("/api/v1/ptp-agent/sessions", headers=HEADERS)
        assert resp.status_code == 201


# ============================================================
# 4.9 搜索会话（放在 4.3 之前测试路由优先级）
# ============================================================


class TestSearchSessions:
    def test_ok(self, client, mock_repo) -> None:
        mock_repo.search_sessions.return_value = [_session_dict(title="三路匹配")]
        resp = client.get("/api/v1/ptp-agent/sessions/search?q=三路", headers=HEADERS)
        assert resp.status_code == 200
        assert len(resp.json()["sessions"]) == 1

    def test_empty_q(self, client) -> None:
        resp = client.get("/api/v1/ptp-agent/sessions/search?q=", headers=HEADERS)
        assert resp.status_code == 422  # validation error


# ============================================================
# 4.3 获取会话详情
# ============================================================


class TestGetSessionDetail:
    def test_ok(self, client, mock_repo) -> None:
        mock_repo.get_session_with_messages.return_value = {
            "session": _session_dict(),
            "messages": [_message_dict()],
            "has_more_messages": False,
        }
        resp = client.get("/api/v1/ptp-agent/sessions/s-001", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["messages"]) == 1

    def test_not_found(self, client, mock_repo) -> None:
        mock_repo.get_session_with_messages.return_value = None
        resp = client.get("/api/v1/ptp-agent/sessions/no-id", headers=HEADERS)
        assert resp.status_code == 404


# ============================================================
# 4.4 更新标题
# ============================================================


class TestUpdateTitle:
    def test_ok(self, client, mock_repo) -> None:
        mock_repo.update_title.return_value = _session_dict(title="新标题", title_auto=False)
        resp = client.patch(
            "/api/v1/ptp-agent/sessions/s-001",
            headers=HEADERS,
            json={"title": "新标题"},
        )
        assert resp.status_code == 200
        assert resp.json()["session"]["title"] == "新标题"

    def test_not_found(self, client, mock_repo) -> None:
        mock_repo.update_title.return_value = None
        resp = client.patch(
            "/api/v1/ptp-agent/sessions/no-id",
            headers=HEADERS,
            json={"title": "x"},
        )
        assert resp.status_code == 404


# ============================================================
# 4.5 删除会话
# ============================================================


class TestDeleteSession:
    def test_ok(self, client, mock_repo) -> None:
        mock_repo.delete_session.return_value = True
        resp = client.delete("/api/v1/ptp-agent/sessions/s-001", headers=HEADERS)
        assert resp.status_code == 204

    def test_not_found(self, client, mock_repo) -> None:
        mock_repo.delete_session.return_value = False
        resp = client.delete("/api/v1/ptp-agent/sessions/no-id", headers=HEADERS)
        assert resp.status_code == 404


# ============================================================
# 4.6 清空全部
# ============================================================


class TestClearAll:
    def test_ok(self, client, mock_repo) -> None:
        mock_repo.delete_all_sessions.return_value = 5
        resp = client.request(
            "DELETE",
            "/api/v1/ptp-agent/sessions",
            headers=HEADERS,
            json={"confirm": "DELETE_ALL"},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted_count"] == 5

    def test_missing_confirm(self, client, mock_repo) -> None:
        resp = client.request(
            "DELETE",
            "/api/v1/ptp-agent/sessions",
            headers=HEADERS,
            json={"confirm": "wrong"},
        )
        assert resp.status_code == 400


# ============================================================
# 4.7 追加消息
# ============================================================


class TestAppendMessages:
    def test_ok(self, client, mock_repo) -> None:
        mock_repo.append_messages.return_value = {
            "messages": [_message_dict()],
            "session": _session_dict(message_count=1),
        }
        resp = client.post(
            "/api/v1/ptp-agent/sessions/s-001/messages",
            headers=HEADERS,
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        assert resp.status_code == 201
        assert len(resp.json()["messages"]) == 1

    def test_not_found(self, client, mock_repo) -> None:
        mock_repo.append_messages.return_value = None
        resp = client.post(
            "/api/v1/ptp-agent/sessions/no-id/messages",
            headers=HEADERS,
            json={"messages": [{"role": "user", "content": "x"}]},
        )
        assert resp.status_code == 404

    def test_session_full(self, client, mock_repo) -> None:
        mock_repo.append_messages.side_effect = ValueError("SESSION_FULL: 超限")
        resp = client.post(
            "/api/v1/ptp-agent/sessions/s-001/messages",
            headers=HEADERS,
            json={"messages": [{"role": "user", "content": "x"}]},
        )
        assert resp.status_code == 409

    def test_content_too_large(self, client, mock_repo) -> None:
        mock_repo.append_messages.side_effect = ValueError("CONTENT_TOO_LARGE: 超限")
        resp = client.post(
            "/api/v1/ptp-agent/sessions/s-001/messages",
            headers=HEADERS,
            json={"messages": [{"role": "user", "content": "x"}]},
        )
        assert resp.status_code == 413

    def test_invalid_role(self, client, mock_repo) -> None:
        resp = client.post(
            "/api/v1/ptp-agent/sessions/s-001/messages",
            headers=HEADERS,
            json={"messages": [{"role": "system", "content": "x"}]},
        )
        assert resp.status_code == 422


# ============================================================
# 4.8 更新消息
# ============================================================


class TestUpdateMessage:
    def test_ok(self, client, mock_repo) -> None:
        mock_repo.update_message.return_value = _message_dict(content="updated")
        resp = client.patch(
            "/api/v1/ptp-agent/sessions/s-001/messages/m-001",
            headers=HEADERS,
            json={"content": "updated"},
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == "updated"

    def test_not_found(self, client, mock_repo) -> None:
        mock_repo.update_message.return_value = None
        resp = client.patch(
            "/api/v1/ptp-agent/sessions/s-001/messages/no-mid",
            headers=HEADERS,
            json={"content": "x"},
        )
        assert resp.status_code == 404
