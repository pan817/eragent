"""历史 chat_messages 回填 Chroma chat_history 索引。

手动运行，不自动执行。支持断点续传、dry-run、按用户过滤。

运行:
    python scripts/backfill_chat_index.py [--batch-size 100] [--since 2026-01-01]
                                          [--user-id U123] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_logger = logging.getLogger(__name__)

_CHECKPOINT_FILE = ".backfill_chat_index_checkpoint"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill chat_messages into Chroma chat_history index")
    p.add_argument("--batch-size", type=int, default=100, help="Messages per batch")
    p.add_argument("--since", type=str, default=None, help="ISO date lower bound (e.g. 2026-01-01)")
    p.add_argument("--user-id", type=str, default=None, help="Filter by user_id")
    p.add_argument("--dry-run", action="store_true", help="Print stats without writing to Chroma")
    return p.parse_args()


def _load_checkpoint() -> str | None:
    try:
        with open(_CHECKPOINT_FILE) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def _save_checkpoint(ts: str) -> None:
    with open(_CHECKPOINT_FILE, "w") as f:
        f.write(ts)


def main() -> None:
    args = _parse_args()

    sys.path.insert(0, ".")
    from config.settings import get_settings
    from core.chat.tables import chat_messages_table, chat_sessions_table
    from core.knowledge.vector_store import VectorStore

    settings = get_settings()
    dsn = settings.postgresql.dsn
    engine = create_engine(dsn, pool_pre_ping=True)
    sf = sessionmaker(bind=engine)

    checkpoint = _load_checkpoint()
    since = None
    if args.since:
        since = datetime.fromisoformat(args.since)
    if checkpoint:
        cp_dt = datetime.fromisoformat(checkpoint)
        if since is None or cp_dt > since:
            since = cp_dt
            _logger.info("Resuming from checkpoint: %s", since.isoformat())

    store: VectorStore | None = None
    if not args.dry_run:
        store = VectorStore.from_settings(settings, "chat_history")
        store.initialize()
        _logger.info("Chroma chat_history collection ready")

    filters = [chat_messages_table.c.session_id == chat_sessions_table.c.id]
    if since:
        filters.append(chat_messages_table.c.created_at > since)
    if args.user_id:
        filters.append(chat_sessions_table.c.user_id == args.user_id)

    count_stmt = (
        select(sa.func.count())
        .select_from(chat_messages_table.join(
            chat_sessions_table,
            chat_messages_table.c.session_id == chat_sessions_table.c.id,
        ))
        .where(*filters)
    )
    with sf() as s:
        total = s.execute(count_stmt).scalar() or 0
    _logger.info("Total messages to process: %d", total)

    if total == 0:
        _logger.info("Nothing to backfill")
        return

    stmt = (
        select(
            chat_messages_table.c.id,
            chat_messages_table.c.session_id,
            chat_messages_table.c.role,
            chat_messages_table.c.content,
            chat_messages_table.c.created_at,
            chat_sessions_table.c.user_id,
        )
        .select_from(chat_messages_table.join(
            chat_sessions_table,
            chat_messages_table.c.session_id == chat_sessions_table.c.id,
        ))
        .where(*filters)
        .order_by(chat_messages_table.c.created_at.asc())
    )

    processed = 0
    fragment_count = 0
    batch_size = args.batch_size
    last_ts: str | None = None

    with sf() as s:
        result = s.execute(stmt)
        session_buffer: dict[str, list[dict]] = {}

        for row in result:
            msg = {
                "id": row.id,
                "session_id": row.session_id,
                "role": row.role,
                "content": row.content,
                "created_at": row.created_at,
                "user_id": row.user_id,
            }
            session_buffer.setdefault(row.session_id, []).append(msg)
            processed += 1

            if processed % batch_size == 0:
                fc = _flush_buffer(session_buffer, store, args.dry_run)
                fragment_count += fc
                last_ts = row.created_at.isoformat() if row.created_at else last_ts
                if last_ts:
                    _save_checkpoint(last_ts)
                session_buffer.clear()
                _logger.info("Processed %d/%d messages, %d fragments", processed, total, fragment_count)

        if session_buffer:
            fc = _flush_buffer(session_buffer, store, args.dry_run)
            fragment_count += fc

    if last_ts:
        _save_checkpoint(last_ts)

    _logger.info(
        "Backfill complete: %d messages -> %d fragments%s",
        processed,
        fragment_count,
        " (dry-run)" if args.dry_run else "",
    )


def _flush_buffer(
    buffer: dict[str, list[dict]],
    store: VectorStore | None,
    dry_run: bool,
) -> int:
    """将 buffer 中的消息聚合成片段并写入 Chroma。"""
    fragment_count = 0
    for session_id, msgs in buffer.items():
        msgs.sort(key=lambda m: m["created_at"])
        chunk: list[dict] = []
        for msg in msgs:
            chunk.append(msg)
            if msg["role"] == "assistant" and len(chunk) >= 2:
                _write_fragment(session_id, chunk, store, dry_run)
                fragment_count += 1
                chunk = []
            elif len(chunk) >= 6:
                _write_fragment(session_id, chunk, store, dry_run)
                fragment_count += 1
                chunk = []
        if chunk:
            _write_fragment(session_id, chunk, store, dry_run)
            fragment_count += 1
    return fragment_count


def _write_fragment(
    session_id: str,
    chunk: list[dict],
    store: VectorStore | None,
    dry_run: bool,
) -> None:
    roles = [m["role"] for m in chunk]
    content_parts = [f"[{m['role']}] {m['content']}" for m in chunk]
    doc_id = f"chat_{session_id}_{chunk[0]['id']}"
    document = "\n".join(content_parts)
    metadata = {
        "user_id": chunk[0]["user_id"],
        "session_id": session_id,
        "message_ids": ",".join(m["id"] for m in chunk),
        "role_pattern": "→".join(roles),
        "created_at": chunk[0]["created_at"].isoformat(),
        "record_type": "chat_fragment",
    }
    if dry_run:
        _logger.debug("DRY-RUN: would write fragment %s (%d chars)", doc_id, len(document))
        return

    collection = store._ensure_collection()
    collection.upsert(
        ids=[doc_id],
        documents=[document],
        metadatas=[metadata],
    )


if __name__ == "__main__":
    main()
