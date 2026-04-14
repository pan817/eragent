"""会话历史数据访问层（Repository）。

提供 chat_sessions / chat_messages 的 CRUD 操作，
所有方法均为同步，由路由层通过 asyncio.to_thread 调用。
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from core.chat.tables import chat_messages_table, chat_sessions_table
from core.logging_utils import get_logger
from core.time_utils import now_cn

_logger = get_logger(__name__)

# 常量
_MAX_EMPTY_SESSIONS = 3
_MAX_MESSAGES_PER_SESSION = 500
_MAX_CONTENT_BYTES = 32 * 1024  # 32KB
_TITLE_AUTO_LENGTH = 24
_PREVIEW_LENGTH = 60


def _now() -> datetime:
    return now_cn()


def _new_id() -> str:
    return str(uuid.uuid4())


class ChatRepository:
    """会话历史 CRUD 仓库。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------
    # 按指定 ID 创建会话（供 /analyze 自动落库使用）
    # ------------------------------------------------------------------

    def create_session_with_id(
        self, user_id: str, session_id: str, title: str = "新对话"
    ) -> dict[str, Any]:
        """用指定 session_id 创建会话（幂等：已存在则直接返回）。"""
        with self._sf() as s:
            existing = s.execute(
                select(chat_sessions_table).where(
                    chat_sessions_table.c.id == session_id,
                    chat_sessions_table.c.deleted_at.is_(None),
                )
            ).fetchone()
            if existing is not None:
                return self._row_to_session(existing)

            now = _now()
            s.execute(
                chat_sessions_table.insert().values(
                    id=session_id,
                    user_id=user_id,
                    title=title,
                    title_auto=True,
                    message_count=0,
                    last_message_preview=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            s.commit()
            row = s.execute(
                select(chat_sessions_table).where(
                    chat_sessions_table.c.id == session_id
                )
            ).fetchone()
            return self._row_to_session(row)

    # ------------------------------------------------------------------
    # 4.2 创建会话
    # ------------------------------------------------------------------

    def create_session(self, user_id: str, title: str = "新对话") -> dict[str, Any]:
        """创建新会话。空会话数达上限时返回最老的空会话。"""
        with self._sf() as s:
            # 查找已有的空会话
            empty = s.execute(
                select(chat_sessions_table)
                .where(
                    chat_sessions_table.c.user_id == user_id,
                    chat_sessions_table.c.message_count == 0,
                    chat_sessions_table.c.deleted_at.is_(None),
                )
                .order_by(chat_sessions_table.c.created_at)
            ).fetchall()

            if len(empty) >= _MAX_EMPTY_SESSIONS:
                row = empty[0]
                return self._row_to_session(row)

            now = _now()
            sid = _new_id()
            s.execute(
                chat_sessions_table.insert().values(
                    id=sid,
                    user_id=user_id,
                    title=title,
                    title_auto=True,
                    message_count=0,
                    last_message_preview=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            s.commit()

            row = s.execute(
                select(chat_sessions_table).where(
                    chat_sessions_table.c.id == sid
                )
            ).fetchone()
            return self._row_to_session(row)

    # ------------------------------------------------------------------
    # 4.3 获取会话详情（含消息）
    # ------------------------------------------------------------------

    def get_session(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        """获取会话元信息（不含消息），不存在或不属于该用户返回 None。"""
        with self._sf() as s:
            row = s.execute(
                select(chat_sessions_table).where(
                    chat_sessions_table.c.id == session_id,
                    chat_sessions_table.c.user_id == user_id,
                    chat_sessions_table.c.deleted_at.is_(None),
                )
            ).fetchone()
            if row is None:
                return None
            return self._row_to_session(row)

    def get_session_with_messages(
        self,
        user_id: str,
        session_id: str,
        message_limit: int = 200,
    ) -> dict[str, Any] | None:
        """获取会话详情 + 消息列表。"""
        session_data = self.get_session(user_id, session_id)
        if session_data is None:
            return None

        with self._sf() as s:
            total_count = s.execute(
                select(func.count())
                .select_from(chat_messages_table)
                .where(chat_messages_table.c.session_id == session_id)
            ).scalar() or 0

            rows = s.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.session_id == session_id)
                .order_by(
                    chat_messages_table.c.created_at.desc(),
                    chat_messages_table.c.id.desc(),
                )
                .limit(message_limit)
            ).fetchall()

            # 反转为正序
            messages = [self._row_to_message(r) for r in reversed(rows)]

        return {
            "session": session_data,
            "messages": messages,
            "has_more_messages": total_count > message_limit,
        }

    # ------------------------------------------------------------------
    # 4.1 列出会话（cursor 分页）
    # ------------------------------------------------------------------

    def list_sessions(
        self,
        user_id: str,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """按 updated_at DESC 列出会话，支持 cursor 分页。"""
        if limit < 1:
            limit = 1
        elif limit > 50:
            limit = 50

        with self._sf() as s:
            stmt = (
                select(chat_sessions_table)
                .where(
                    chat_sessions_table.c.user_id == user_id,
                    chat_sessions_table.c.deleted_at.is_(None),
                )
            )

            if cursor:
                cursor_updated_at, cursor_id = self._decode_cursor(cursor)
                if cursor_updated_at is not None:
                    t = chat_sessions_table.c
                    stmt = stmt.where(
                        (t.updated_at < cursor_updated_at)
                        | (
                            (t.updated_at == cursor_updated_at)
                            & (t.id < cursor_id)
                        )
                    )

            stmt = stmt.order_by(
                chat_sessions_table.c.updated_at.desc(),
                chat_sessions_table.c.id.desc(),
            ).limit(limit + 1)  # 多取一条判断是否有下页

            rows = s.execute(stmt).fetchall()

            has_next = len(rows) > limit
            page_rows = rows[:limit]

            sessions = [self._row_to_session(r) for r in page_rows]
            next_cursor = None
            if has_next and page_rows:
                last = page_rows[-1]
                next_cursor = self._encode_cursor(last.updated_at, last.id)

            # total count
            total = s.execute(
                select(func.count())
                .select_from(chat_sessions_table)
                .where(
                    chat_sessions_table.c.user_id == user_id,
                    chat_sessions_table.c.deleted_at.is_(None),
                )
            ).scalar() or 0

        return {
            "sessions": sessions,
            "next_cursor": next_cursor,
            "total": total,
        }

    # ------------------------------------------------------------------
    # 4.4 更新会话标题
    # ------------------------------------------------------------------

    def update_title(
        self, user_id: str, session_id: str, title: str
    ) -> dict[str, Any] | None:
        """手动更新标题，置 title_auto=False。

        title 为空或 None 时恢复自动推导模式。
        """
        with self._sf() as s:
            row = s.execute(
                select(chat_sessions_table).where(
                    chat_sessions_table.c.id == session_id,
                    chat_sessions_table.c.user_id == user_id,
                    chat_sessions_table.c.deleted_at.is_(None),
                )
            ).fetchone()
            if row is None:
                return None

            now = _now()
            if title:
                s.execute(
                    update(chat_sessions_table)
                    .where(chat_sessions_table.c.id == session_id)
                    .values(title=title[:120], title_auto=False, updated_at=now)
                )
            else:
                # 恢复自动推导
                auto_title = self._derive_title_from_first_message(s, session_id)
                s.execute(
                    update(chat_sessions_table)
                    .where(chat_sessions_table.c.id == session_id)
                    .values(
                        title=auto_title or "新对话",
                        title_auto=True,
                        updated_at=now,
                    )
                )
            s.commit()
            return self.get_session(user_id, session_id)

    @staticmethod
    def _derive_title_from_first_message(s: Session, session_id: str) -> str | None:
        """从首条 user 消息推导标题。"""
        row = s.execute(
            select(chat_messages_table.c.content)
            .where(
                chat_messages_table.c.session_id == session_id,
                chat_messages_table.c.role == "user",
            )
            .order_by(chat_messages_table.c.created_at, chat_messages_table.c.id)
            .limit(1)
        ).fetchone()
        if row and row.content:
            return row.content[:_TITLE_AUTO_LENGTH]
        return None

    # ------------------------------------------------------------------
    # 4.5 删除单个会话（软删除）
    # ------------------------------------------------------------------

    def delete_session(self, user_id: str, session_id: str) -> bool:
        """软删除会话，返回是否找到并删除。"""
        with self._sf() as s:
            result = s.execute(
                update(chat_sessions_table)
                .where(
                    chat_sessions_table.c.id == session_id,
                    chat_sessions_table.c.user_id == user_id,
                    chat_sessions_table.c.deleted_at.is_(None),
                )
                .values(deleted_at=_now())
            )
            s.commit()
            return result.rowcount > 0

    # ------------------------------------------------------------------
    # 4.6 清空用户全部会话（软删除）
    # ------------------------------------------------------------------

    def delete_all_sessions(self, user_id: str) -> int:
        """软删除用户全部会话，返回删除数量。"""
        with self._sf() as s:
            result = s.execute(
                update(chat_sessions_table)
                .where(
                    chat_sessions_table.c.user_id == user_id,
                    chat_sessions_table.c.deleted_at.is_(None),
                )
                .values(deleted_at=_now())
            )
            s.commit()
            return result.rowcount

    # ------------------------------------------------------------------
    # 4.7 追加消息
    # ------------------------------------------------------------------

    def append_messages(
        self,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """批量追加消息到会话，同一事务内完成：
        1. 校验会话存在且属于当前用户
        2. 检查消息数上限
        3. 写入消息
        4. 自动推导标题（首次追加 + title_auto）
        5. 更新 session 冗余字段
        返回 {"messages": [...], "session": {...}} 或 None（会话不存在）。
        """
        with self._sf() as s:
            row = s.execute(
                select(chat_sessions_table).where(
                    chat_sessions_table.c.id == session_id,
                    chat_sessions_table.c.user_id == user_id,
                    chat_sessions_table.c.deleted_at.is_(None),
                )
            ).fetchone()
            if row is None:
                return None

            current_count = row.message_count
            if current_count + len(messages) > _MAX_MESSAGES_PER_SESSION:
                raise ValueError(
                    f"SESSION_FULL: 当前 {current_count} 条，"
                    f"追加 {len(messages)} 条后超过上限 {_MAX_MESSAGES_PER_SESSION}"
                )

            now = _now()
            created_msgs = []
            for i, msg in enumerate(messages):
                content = msg["content"]
                if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
                    raise ValueError(
                        f"CONTENT_TOO_LARGE: 消息内容超过 {_MAX_CONTENT_BYTES // 1024}KB"
                    )
                mid = _new_id()
                ts = now + timedelta(microseconds=i)
                s.execute(
                    chat_messages_table.insert().values(
                        id=mid,
                        session_id=session_id,
                        role=msg["role"],
                        content=content,
                        status=msg.get("status", "success"),
                        duration_ms=msg.get("duration_ms"),
                        trace_id=msg.get("trace_id"),
                        metadata_=msg.get("metadata"),
                        created_at=ts,
                    )
                )
                created_msgs.append({
                    "id": mid,
                    "client_id": msg.get("client_id"),
                    "session_id": session_id,
                    "role": msg["role"],
                    "content": content,
                    "status": msg.get("status", "success"),
                    "duration_ms": msg.get("duration_ms"),
                    "trace_id": msg.get("trace_id"),
                    "created_at": ts.isoformat(),
                    "metadata": msg.get("metadata"),
                })

            # 副作用：标题推导
            new_title = row.title
            if row.title_auto and current_count == 0:
                first_user = next(
                    (m for m in messages if m["role"] == "user"), None
                )
                if first_user:
                    new_title = first_user["content"][:_TITLE_AUTO_LENGTH]

            # 副作用：last_message_preview
            last_content = messages[-1]["content"] if messages else None
            preview = last_content[:_PREVIEW_LENGTH] if last_content else row.last_message_preview

            new_count = current_count + len(messages)
            s.execute(
                update(chat_sessions_table)
                .where(chat_sessions_table.c.id == session_id)
                .values(
                    title=new_title,
                    message_count=new_count,
                    last_message_preview=preview,
                    updated_at=now,
                )
            )
            s.commit()

        updated_session = self.get_session(user_id, session_id)
        return {
            "messages": created_msgs,
            "session": updated_session,
        }

    # ------------------------------------------------------------------
    # 4.8 更新消息
    # ------------------------------------------------------------------

    def update_message(
        self,
        user_id: str,
        session_id: str,
        message_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """部分更新消息内容/状态/元信息。role 和 created_at 不可修改。"""
        # 先校验 session 归属
        session_data = self.get_session(user_id, session_id)
        if session_data is None:
            return None

        allowed = {"content", "status", "duration_ms", "trace_id", "metadata"}
        values = {}
        for k, v in updates.items():
            if k in allowed:
                col_name = "metadata_" if k == "metadata" else k
                values[col_name] = v

        if not values:
            # 无有效字段，直接返回当前消息
            return self._get_message(session_id, message_id)

        if "content" in values and len(values["content"].encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise ValueError(
                f"CONTENT_TOO_LARGE: 消息内容超过 {_MAX_CONTENT_BYTES // 1024}KB"
            )

        with self._sf() as s:
            result = s.execute(
                update(chat_messages_table)
                .where(
                    chat_messages_table.c.id == message_id,
                    chat_messages_table.c.session_id == session_id,
                )
                .values(**values)
            )
            if result.rowcount == 0:
                return None

            # 更新 session.updated_at + last_message_preview（如果改的是最后一条）
            now = _now()
            session_updates: dict[str, Any] = {"updated_at": now}

            if "content" in values:
                last_msg = s.execute(
                    select(chat_messages_table.c.id)
                    .where(chat_messages_table.c.session_id == session_id)
                    .order_by(
                        chat_messages_table.c.created_at.desc(),
                        chat_messages_table.c.id.desc(),
                    )
                    .limit(1)
                ).fetchone()
                if last_msg and last_msg.id == message_id:
                    session_updates["last_message_preview"] = values["content"][:_PREVIEW_LENGTH]

            s.execute(
                update(chat_sessions_table)
                .where(chat_sessions_table.c.id == session_id)
                .values(**session_updates)
            )
            s.commit()

        return self._get_message(session_id, message_id)

    def _get_message(self, session_id: str, message_id: str) -> dict[str, Any] | None:
        with self._sf() as s:
            row = s.execute(
                select(chat_messages_table).where(
                    chat_messages_table.c.id == message_id,
                    chat_messages_table.c.session_id == session_id,
                )
            ).fetchone()
            if row is None:
                return None
            return self._row_to_message(row)

    # ------------------------------------------------------------------
    # 4.9 搜索会话
    # ------------------------------------------------------------------

    def search_sessions(
        self,
        user_id: str,
        q: str,
        limit: int = 20,
        scope: str = "all",
    ) -> list[dict[str, Any]]:
        """按关键词搜索会话（ILIKE），支持 title / content / all。"""
        if limit < 1:
            limit = 1
        elif limit > 50:
            limit = 50

        pattern = f"%{q}%"

        with self._sf() as s:
            if scope == "title":
                # 仅搜标题
                rows = s.execute(
                    select(chat_sessions_table)
                    .where(
                        chat_sessions_table.c.user_id == user_id,
                        chat_sessions_table.c.deleted_at.is_(None),
                        chat_sessions_table.c.title.ilike(pattern),
                    )
                    .order_by(chat_sessions_table.c.updated_at.desc())
                    .limit(limit)
                ).fetchall()
                return [self._row_to_session(r) for r in rows]

            elif scope == "content":
                # 仅搜消息内容
                session_ids = s.execute(
                    select(chat_messages_table.c.session_id)
                    .where(chat_messages_table.c.content.ilike(pattern))
                    .distinct()
                ).scalars().all()

                if not session_ids:
                    return []

                rows = s.execute(
                    select(chat_sessions_table)
                    .where(
                        chat_sessions_table.c.user_id == user_id,
                        chat_sessions_table.c.deleted_at.is_(None),
                        chat_sessions_table.c.id.in_(session_ids),
                    )
                    .order_by(chat_sessions_table.c.updated_at.desc())
                    .limit(limit)
                ).fetchall()
                return [self._row_to_session(r) for r in rows]

            else:
                # scope=all: 搜标题 + 消息内容
                title_ids = s.execute(
                    select(chat_sessions_table.c.id)
                    .where(
                        chat_sessions_table.c.user_id == user_id,
                        chat_sessions_table.c.deleted_at.is_(None),
                        chat_sessions_table.c.title.ilike(pattern),
                    )
                ).scalars().all()

                content_ids = s.execute(
                    select(chat_messages_table.c.session_id)
                    .where(chat_messages_table.c.content.ilike(pattern))
                    .distinct()
                ).scalars().all()

                all_ids = list(set(title_ids) | set(content_ids))
                if not all_ids:
                    return []

                rows = s.execute(
                    select(chat_sessions_table)
                    .where(
                        chat_sessions_table.c.user_id == user_id,
                        chat_sessions_table.c.deleted_at.is_(None),
                        chat_sessions_table.c.id.in_(all_ids),
                    )
                    .order_by(chat_sessions_table.c.updated_at.desc())
                    .limit(limit)
                ).fetchall()
                return [self._row_to_session(r) for r in rows]

    @staticmethod
    def _encode_cursor(updated_at: datetime, session_id: str) -> str:
        payload = {
            "u": updated_at.isoformat(),
            "i": session_id,
        }
        return base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).decode()

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[datetime | None, str]:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor))
            return datetime.fromisoformat(payload["u"]), payload["i"]
        except Exception:
            _logger.warning("invalid cursor: %s", cursor)
            return None, ""

    # ------------------------------------------------------------------
    # 辅助：行转字典
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_session(row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "title": row.title,
            "title_auto": row.title_auto,
            "message_count": row.message_count,
            "last_message_preview": row.last_message_preview,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _row_to_message(row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "session_id": row.session_id,
            "role": row.role,
            "content": row.content,
            "status": row.status,
            "duration_ms": row.duration_ms,
            "trace_id": row.trace_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "metadata": row.metadata_,
        }
