"""ChatRepository 单元测试。

使用 SQLite 内存库，独立于其他测试，不依赖外部服务。
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool

from core.chat.repository import ChatRepository
from core.chat.tables import chat_messages_table, chat_sessions_table
from core.database.engine import create_engine_from_dsn, get_session_factory


@pytest.fixture()
def chat_repo():
    """创建 SQLite 内存库 + ChatRepository。"""
    engine = create_engine_from_dsn(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # 只建 chat 相关表
    from core.chat.tables import metadata_obj

    metadata_obj.create_all(engine)
    sf = get_session_factory(engine)
    repo = ChatRepository(sf)
    yield repo
    engine.dispose()


# ============================================================
# create_session
# ============================================================


class TestCreateSession:
    def test_create_basic(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        assert s["user_id"] == "alice"
        assert s["title"] == "新对话"
        assert s["title_auto"] is True
        assert s["message_count"] == 0

    def test_create_with_title(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice", title="自定义")
        assert s["title"] == "自定义"

    def test_empty_session_limit(self, chat_repo: ChatRepository) -> None:
        """空会话超过 3 个时返回最老的空会话。"""
        s1 = chat_repo.create_session("alice")
        s2 = chat_repo.create_session("alice")
        s3 = chat_repo.create_session("alice")
        s4 = chat_repo.create_session("alice")
        # 第 4 次应返回 s1（最老的空会话）
        assert s4["id"] == s1["id"]


# ============================================================
# create_session_with_id
# ============================================================


class TestCreateSessionWithId:
    def test_create_with_specific_id(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session_with_id("alice", "my-custom-id")
        assert s["id"] == "my-custom-id"
        assert s["user_id"] == "alice"

    def test_idempotent(self, chat_repo: ChatRepository) -> None:
        """同一 id 重复创建返回已有的。"""
        s1 = chat_repo.create_session_with_id("alice", "same-id")
        s2 = chat_repo.create_session_with_id("alice", "same-id")
        assert s1["id"] == s2["id"]


# ============================================================
# get_session
# ============================================================


class TestGetSession:
    def test_get_existing(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        got = chat_repo.get_session("alice", s["id"])
        assert got is not None
        assert got["id"] == s["id"]

    def test_get_nonexistent(self, chat_repo: ChatRepository) -> None:
        assert chat_repo.get_session("alice", "no-such-id") is None

    def test_get_other_user(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        assert chat_repo.get_session("bob", s["id"]) is None


# ============================================================
# get_session_with_messages
# ============================================================


class TestGetSessionWithMessages:
    def test_empty_session(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        result = chat_repo.get_session_with_messages("alice", s["id"])
        assert result is not None
        assert result["messages"] == []
        assert result["has_more_messages"] is False

    def test_with_messages(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        chat_repo.append_messages("alice", s["id"], [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ])
        result = chat_repo.get_session_with_messages("alice", s["id"])
        assert len(result["messages"]) == 2
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][1]["role"] == "assistant"

    def test_message_limit(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        msgs = [{"role": "user", "content": f"msg-{i}"} for i in range(5)]
        chat_repo.append_messages("alice", s["id"], msgs)
        result = chat_repo.get_session_with_messages("alice", s["id"], message_limit=3)
        assert len(result["messages"]) == 3
        assert result["has_more_messages"] is True

    def test_nonexistent(self, chat_repo: ChatRepository) -> None:
        assert chat_repo.get_session_with_messages("alice", "no-id") is None


# ============================================================
# list_sessions
# ============================================================


class TestListSessions:
    def test_empty(self, chat_repo: ChatRepository) -> None:
        result = chat_repo.list_sessions("alice")
        assert result["sessions"] == []
        assert result["total"] == 0
        assert result["next_cursor"] is None

    def test_basic_list(self, chat_repo: ChatRepository) -> None:
        chat_repo.create_session("alice")
        chat_repo.create_session("alice", "第二个")
        result = chat_repo.list_sessions("alice")
        assert result["total"] == 2
        assert len(result["sessions"]) == 2

    def test_user_isolation(self, chat_repo: ChatRepository) -> None:
        chat_repo.create_session("alice")
        chat_repo.create_session("bob")
        assert chat_repo.list_sessions("alice")["total"] == 1
        assert chat_repo.list_sessions("bob")["total"] == 1

    def test_pagination(self, chat_repo: ChatRepository) -> None:
        # 先消耗空会话限制
        for i in range(3):
            s = chat_repo.create_session(f"user-page")
            chat_repo.append_messages(f"user-page", s["id"], [
                {"role": "user", "content": f"msg-{i}"}
            ])
        # 再创建更多
        for i in range(3, 5):
            s = chat_repo.create_session(f"user-page", f"title-{i}")
            chat_repo.append_messages(f"user-page", s["id"], [
                {"role": "user", "content": f"msg-{i}"}
            ])

        page1 = chat_repo.list_sessions("user-page", limit=2)
        assert len(page1["sessions"]) == 2
        assert page1["next_cursor"] is not None
        assert page1["total"] == 5

        page2 = chat_repo.list_sessions("user-page", limit=2, cursor=page1["next_cursor"])
        assert len(page2["sessions"]) == 2
        assert page2["next_cursor"] is not None

        page3 = chat_repo.list_sessions("user-page", limit=2, cursor=page2["next_cursor"])
        assert len(page3["sessions"]) == 1
        assert page3["next_cursor"] is None


# ============================================================
# update_title
# ============================================================


class TestUpdateTitle:
    def test_update(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        updated = chat_repo.update_title("alice", s["id"], "新标题")
        assert updated is not None
        assert updated["title"] == "新标题"
        assert updated["title_auto"] is False

    def test_reset_auto(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        chat_repo.update_title("alice", s["id"], "手动标题")
        updated = chat_repo.update_title("alice", s["id"], "")
        assert updated["title_auto"] is True

    def test_nonexistent(self, chat_repo: ChatRepository) -> None:
        assert chat_repo.update_title("alice", "no-id", "x") is None


# ============================================================
# delete_session
# ============================================================


class TestDeleteSession:
    def test_delete(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        assert chat_repo.delete_session("alice", s["id"]) is True
        assert chat_repo.get_session("alice", s["id"]) is None

    def test_delete_nonexistent(self, chat_repo: ChatRepository) -> None:
        assert chat_repo.delete_session("alice", "no-id") is False

    def test_delete_other_user(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        assert chat_repo.delete_session("bob", s["id"]) is False


# ============================================================
# delete_all_sessions
# ============================================================


class TestDeleteAllSessions:
    def test_delete_all(self, chat_repo: ChatRepository) -> None:
        chat_repo.create_session("alice")
        # 需要先给 session 追加消息使其非空，才能再创建新的
        s1 = chat_repo.create_session("alice")
        count = chat_repo.delete_all_sessions("alice")
        assert count >= 1
        assert chat_repo.list_sessions("alice")["total"] == 0

    def test_user_isolation(self, chat_repo: ChatRepository) -> None:
        chat_repo.create_session("alice")
        chat_repo.create_session("bob")
        chat_repo.delete_all_sessions("alice")
        assert chat_repo.list_sessions("alice")["total"] == 0
        assert chat_repo.list_sessions("bob")["total"] == 1


# ============================================================
# append_messages
# ============================================================


class TestAppendMessages:
    def test_append_basic(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        result = chat_repo.append_messages("alice", s["id"], [
            {"role": "user", "content": "hello world 你好世界"},
        ])
        assert result is not None
        assert len(result["messages"]) == 1
        assert result["messages"][0]["role"] == "user"
        assert result["session"]["message_count"] == 1

    def test_auto_title(self, chat_repo: ChatRepository) -> None:
        """首条 user 消息自动推导标题。"""
        s = chat_repo.create_session("alice")
        chat_repo.append_messages("alice", s["id"], [
            {"role": "user", "content": "分析最近三路匹配异常的情况，请给出详细报告"},
        ])
        updated = chat_repo.get_session("alice", s["id"])
        # 前 24 字
        assert updated["title"] == "分析最近三路匹配异常的情况，请给出详细报告"[:24]

    def test_preview_updated(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        chat_repo.append_messages("alice", s["id"], [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "回答内容"},
        ])
        updated = chat_repo.get_session("alice", s["id"])
        assert updated["last_message_preview"] == "回答内容"

    def test_nonexistent_session(self, chat_repo: ChatRepository) -> None:
        assert chat_repo.append_messages("alice", "no-id", [
            {"role": "user", "content": "x"}
        ]) is None

    def test_session_full(self, chat_repo: ChatRepository) -> None:
        """超过 500 条消息上限时应报错。"""
        s = chat_repo.create_session("alice")
        # 先塞 498 条
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(498)]
        chat_repo.append_messages("alice", s["id"], msgs)
        # 再追加 3 条应失败（498+3=501 > 500）
        with pytest.raises(ValueError, match="SESSION_FULL"):
            chat_repo.append_messages("alice", s["id"], [
                {"role": "user", "content": "x"},
                {"role": "user", "content": "y"},
                {"role": "user", "content": "z"},
            ])

    def test_content_too_large(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        big = "x" * 33000
        with pytest.raises(ValueError, match="CONTENT_TOO_LARGE"):
            chat_repo.append_messages("alice", s["id"], [
                {"role": "user", "content": big}
            ])

    def test_client_id_passthrough(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        result = chat_repo.append_messages("alice", s["id"], [
            {"role": "user", "content": "hi", "client_id": "temp-1"},
        ])
        assert result["messages"][0]["client_id"] == "temp-1"


# ============================================================
# update_message
# ============================================================


class TestUpdateMessage:
    def test_update_content(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        result = chat_repo.append_messages("alice", s["id"], [
            {"role": "assistant", "content": "old"},
        ])
        mid = result["messages"][0]["id"]
        updated = chat_repo.update_message("alice", s["id"], mid, {
            "content": "new content",
            "status": "success",
        })
        assert updated is not None
        assert updated["content"] == "new content"

    def test_update_nonexistent(self, chat_repo: ChatRepository) -> None:
        s = chat_repo.create_session("alice")
        assert chat_repo.update_message("alice", s["id"], "no-mid", {"content": "x"}) is None

    def test_update_wrong_session(self, chat_repo: ChatRepository) -> None:
        """session 归属校验。"""
        s = chat_repo.create_session("alice")
        result = chat_repo.append_messages("alice", s["id"], [
            {"role": "user", "content": "x"},
        ])
        mid = result["messages"][0]["id"]
        assert chat_repo.update_message("bob", s["id"], mid, {"content": "hack"}) is None

    def test_ignore_role_update(self, chat_repo: ChatRepository) -> None:
        """role 不在允许字段中，应被忽略。"""
        s = chat_repo.create_session("alice")
        result = chat_repo.append_messages("alice", s["id"], [
            {"role": "user", "content": "x"},
        ])
        mid = result["messages"][0]["id"]
        updated = chat_repo.update_message("alice", s["id"], mid, {
            "role": "assistant",
        })
        # role 不在 allowed 中，无有效更新，返回原消息
        assert updated is not None
        assert updated["role"] == "user"


# ============================================================
# search_sessions
# ============================================================


class TestSearchSessions:
    def _seed(self, chat_repo: ChatRepository) -> None:
        s1 = chat_repo.create_session("alice", "三路匹配分析")
        chat_repo.append_messages("alice", s1["id"], [
            {"role": "user", "content": "分析 SUP-001 的三路匹配"},
        ])
        s2 = chat_repo.create_session("alice", "价格差异报告")
        chat_repo.append_messages("alice", s2["id"], [
            {"role": "user", "content": "查看供应商价格差异"},
        ])

    def test_search_title(self, chat_repo: ChatRepository) -> None:
        self._seed(chat_repo)
        results = chat_repo.search_sessions("alice", "三路", scope="title")
        assert len(results) == 1
        assert "三路" in results[0]["title"]

    def test_search_content(self, chat_repo: ChatRepository) -> None:
        self._seed(chat_repo)
        results = chat_repo.search_sessions("alice", "SUP-001", scope="content")
        assert len(results) == 1

    def test_search_all(self, chat_repo: ChatRepository) -> None:
        self._seed(chat_repo)
        results = chat_repo.search_sessions("alice", "分析")
        assert len(results) >= 1

    def test_search_no_match(self, chat_repo: ChatRepository) -> None:
        self._seed(chat_repo)
        results = chat_repo.search_sessions("alice", "不存在的关键词")
        assert results == []

    def test_search_user_isolation(self, chat_repo: ChatRepository) -> None:
        self._seed(chat_repo)
        results = chat_repo.search_sessions("bob", "三路")
        assert results == []
