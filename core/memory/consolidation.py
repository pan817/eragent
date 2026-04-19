"""ConsolidationEngine — 记忆整合引擎。

触发判定 + per-user 锁 + 合并/归纳/冲突检测/TTL 淘汰。
支持 LLM 语义合并和纯规则合并两种模式（可配置开关）。
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Any

import sqlalchemy as sa

from config.settings import Settings
from core.memory.tables import memories_table, memory_consolidation_log_table
from core.memory.types import MemoryType
from core.time_utils import now_cn

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 触发判定
# ---------------------------------------------------------------------------


def should_consolidate(
    user_id: str,
    engine: sa.engine.Engine,
    settings: Settings,
) -> bool:
    """判断是否应为该用户触发整合。

    条件 A：自上次整合以来新增记忆 ≥ min_new_memories
    条件 B：距上次整合完成 ≥ max_interval_hours
    A OR B 为 true → 触发。
    """
    cfg = settings.memory.consolidation

    with engine.connect() as conn:
        # 获取上次整合完成时间
        last_completed = conn.execute(
            sa.select(sa.func.max(memory_consolidation_log_table.c.completed_at))
            .where(sa.and_(
                memory_consolidation_log_table.c.user_id == user_id,
                memory_consolidation_log_table.c.status == "completed",
            ))
        ).scalar()

        # 条件 B：时间间隔
        if last_completed is not None:
            hours_since = (now_cn() - last_completed).total_seconds() / 3600
            if hours_since >= cfg.max_interval_hours:
                return True
        else:
            # 从未整合过，检查是否有足够记忆
            pass

        # 条件 A：新增记忆数
        since = last_completed or now_cn() - timedelta(days=365)
        new_count = conn.execute(
            sa.select(sa.func.count())
            .select_from(memories_table)
            .where(sa.and_(
                memories_table.c.user_id == user_id,
                memories_table.c.created_at > since,
            ))
        ).scalar() or 0

        if new_count >= cfg.min_new_memories:
            return True

    return False


# ---------------------------------------------------------------------------
# per-user 整合锁
# ---------------------------------------------------------------------------


def try_acquire_lock(
    user_id: str,
    engine: sa.engine.Engine,
    lock_timeout_seconds: int = 600,
) -> str | None:
    """尝试获取整合锁，返回 log_id 或 None。

    锁语义：该用户在 consolidation_log 中有 status='running' 的行 → 锁被持有。
    超时保护：running 超过 lock_timeout_seconds → 标记 failed，回收锁。
    """
    with engine.begin() as conn:
        running = conn.execute(
            sa.select(
                memory_consolidation_log_table.c.id,
                memory_consolidation_log_table.c.started_at,
            ).where(sa.and_(
                memory_consolidation_log_table.c.user_id == user_id,
                memory_consolidation_log_table.c.status == "running",
            ))
        ).first()

        if running:
            started = running.started_at
            now = now_cn()
            # SQLite returns naive datetimes; normalize both to aware or naive
            if started.tzinfo is None and now.tzinfo is not None:
                started = started.replace(tzinfo=now.tzinfo)
            elif started.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=started.tzinfo)
            elapsed = (now - started).total_seconds()
            if elapsed < lock_timeout_seconds:
                return None  # 锁被持有
            # 超时 → 回收
            conn.execute(
                sa.update(memory_consolidation_log_table)
                .where(memory_consolidation_log_table.c.id == running.id)
                .values(status="failed", error_message="lock timeout")
            )

        log_id = str(uuid.uuid4())
        conn.execute(
            memory_consolidation_log_table.insert().values(
                id=log_id,
                user_id=user_id,
                started_at=now_cn(),
                status="running",
            )
        )
        return log_id


def release_lock(
    log_id: str,
    engine: sa.engine.Engine,
    *,
    status: str = "completed",
    input_count: int = 0,
    merged_count: int = 0,
    pruned_count: int = 0,
    llm_used: bool = False,
    error_message: str | None = None,
    details: dict | None = None,
) -> None:
    """释放整合锁并更新 log 状态。"""
    with engine.begin() as conn:
        conn.execute(
            sa.update(memory_consolidation_log_table)
            .where(memory_consolidation_log_table.c.id == log_id)
            .values(
                status=status,
                completed_at=now_cn(),
                input_count=input_count,
                merged_count=merged_count,
                pruned_count=pruned_count,
                llm_used=llm_used,
                error_message=error_message,
                details=details,
            )
        )


# ---------------------------------------------------------------------------
# 合并规则（纯规则模式）
# ---------------------------------------------------------------------------


def merge_entity_profiles_rule(
    profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按实体 ID 分组合并 entity_profile（纯规则模式）。"""
    by_entity: dict[str, list[dict]] = defaultdict(list)
    for p in profiles:
        eid = (p.get("attrs") or {}).get("entity_id") or p.get("entity_id")
        if eid:
            by_entity[eid].append(p)

    merged = []
    for entity_id, group in by_entity.items():
        if len(group) < 2:
            continue

        group.sort(key=lambda x: x.get("created_at") or "")
        latest = group[-1]

        history_lines = [m.get("content", "")[:200] for m in group[:-1]]
        content = (
            f"{latest.get('content', '')}\n"
            f"历史记录（{len(group) - 1}条）：{'；'.join(history_lines[:3])}"
        )

        merged.append({
            "content": content,
            "attrs": {**(latest.get("attrs") or {}), "observation_count": len(group)},
            "source_ids": [m["id"] for m in group],
            "entity_id": entity_id,
            "memory_type": MemoryType.ENTITY_PROFILE,
        })
    return merged


def merge_analysis_insights_rule(
    insights: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 related_analysis_types 分组归纳 analysis_insight（纯规则模式）。"""
    by_type: dict[str, list[dict]] = defaultdict(list)
    for ins in insights:
        types = (ins.get("attrs") or {}).get("related_analysis_types", [])
        key = ",".join(sorted(types)) if types else "unknown"
        by_type[key].append(ins)

    merged = []
    for _key, group in by_type.items():
        if len(group) < 3:
            continue

        group.sort(key=lambda x: x.get("created_at") or "")
        contents = [m.get("content", "")[:150] for m in group]
        content = f"趋势归纳（{len(group)}条观察）：{'；'.join(contents[:5])}"

        merged.append({
            "content": content,
            "attrs": {
                "pattern_type": "consolidated_trend",
                "observation_count": len(group),
                "related_analysis_types": (group[0].get("attrs") or {}).get(
                    "related_analysis_types", []
                ),
            },
            "source_ids": [m["id"] for m in group],
            "memory_type": MemoryType.ANALYSIS_INSIGHT,
        })
    return merged


# ---------------------------------------------------------------------------
# TTL 淘汰
# ---------------------------------------------------------------------------


def purge_expired(
    user_id: str,
    engine: sa.engine.Engine,
    vector_store: Any = None,
) -> int:
    """删除已过期的记忆，返回删除条数。"""
    stmt = (
        sa.delete(memories_table)
        .where(sa.and_(
            memories_table.c.user_id == user_id,
            memories_table.c.expires_at.isnot(None),
            memories_table.c.expires_at < now_cn(),
        ))
        .returning(memories_table.c.id)
    )
    try:
        with engine.begin() as conn:
            deleted_ids = [row[0] for row in conn.execute(stmt)]
    except Exception as exc:  # noqa: BLE001
        _logger.warning("purge_expired SQL failed: %s", exc)
        return 0

    if deleted_ids and vector_store is not None:
        try:
            vector_store.delete(ids=[f"memory_{mid}" for mid in deleted_ids])
        except Exception as exc:  # noqa: BLE001
            _logger.warning("vector cleanup after purge failed: %s", exc)

    if deleted_ids:
        _logger.info(
            "purged %d expired memories for user=%s", len(deleted_ids), user_id,
        )
    return len(deleted_ids)


# ---------------------------------------------------------------------------
# 主编排
# ---------------------------------------------------------------------------


async def run_consolidation(user_id: str, settings: Settings) -> None:
    """执行一次记忆整合。

    串联：锁获取 → 加载记忆 → 按类型合并 → 写入产物 → TTL 淘汰 → 释放锁。
    所有异常内部捕获。
    """
    from core.database.engine import get_engine

    engine = get_engine(settings.postgresql)
    cfg = settings.memory.consolidation

    log_id = try_acquire_lock(user_id, engine, cfg.lock_timeout_seconds)
    if log_id is None:
        _logger.info("consolidation skipped: lock held for user=%s", user_id)
        return

    input_count = 0
    merged_count = 0
    pruned_count = 0
    llm_used = False

    try:
        # 加载该用户所有未整合记忆
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(memories_table)
                .where(sa.and_(
                    memories_table.c.user_id == user_id,
                    memories_table.c.is_consolidated == False,  # noqa: E712
                ))
                .order_by(memories_table.c.created_at)
            ).fetchall()

        all_memories = [dict(row._mapping) for row in rows]
        input_count = len(all_memories)

        if input_count < 2:
            _logger.info("consolidation skipped: insufficient memories (%d)", input_count)
            release_lock(log_id, engine, status="completed", input_count=input_count)
            return

        # 按类型分桶
        by_type: dict[str, list[dict]] = defaultdict(list)
        for m in all_memories:
            by_type[m.get("memory_type", "")].append(m)

        # 合并 entity_profile
        ep_merged = merge_entity_profiles_rule(
            by_type.get(MemoryType.ENTITY_PROFILE, [])
        )

        # 归纳 analysis_insight
        ai_merged = merge_analysis_insights_rule(
            by_type.get(MemoryType.ANALYSIS_INSIGHT, [])
        )

        all_merged = ep_merged + ai_merged

        # 写入整合产物
        if all_merged:
            with engine.begin() as conn:
                for item in all_merged:
                    mid = str(uuid.uuid4())
                    conn.execute(
                        memories_table.insert().values(
                            id=mid,
                            user_id=user_id,
                            session_id="consolidation",
                            memory_type=item["memory_type"],
                            content=item["content"],
                            attrs=item.get("attrs"),
                            entity_id=item.get("entity_id"),
                            is_consolidated=True,
                            source_ids=item.get("source_ids"),
                            created_at=now_cn(),
                        )
                    )
                    merged_count += 1

                # 标记源记忆的 consolidated_at
                source_ids = []
                for item in all_merged:
                    source_ids.extend(item.get("source_ids", []))
                if source_ids:
                    conn.execute(
                        sa.update(memories_table)
                        .where(memories_table.c.id.in_(source_ids))
                        .values(consolidated_at=now_cn())
                    )

        # TTL 淘汰
        pruned_count = purge_expired(user_id, engine)

        _logger.info(
            "consolidation completed: user=%s input=%d merged=%d pruned=%d",
            user_id, input_count, merged_count, pruned_count,
        )

        release_lock(
            log_id, engine,
            status="completed",
            input_count=input_count,
            merged_count=merged_count,
            pruned_count=pruned_count,
            llm_used=llm_used,
        )

    except Exception as exc:  # noqa: BLE001
        _logger.error("consolidation failed: user=%s error=%s", user_id, exc)
        release_lock(
            log_id, engine,
            status="failed",
            input_count=input_count,
            merged_count=merged_count,
            pruned_count=pruned_count,
            error_message=str(exc),
        )
