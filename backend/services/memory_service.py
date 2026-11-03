"""用户长期偏好的持久化：召回、写入、列表与删除。

存储使用认证库的独立连接池（ea_auth），与订单业务库连接隔离；
所有查询都强制带 owner，owner 只能来自认证主体。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.mysql import insert

from agent_core.memory import MEMORY_VALUES, MemoryFact, MemoryTurn, extract_memories
from backend.config import get_settings
from backend.database import auth_session_factory
from backend.models.database_models import AgentMemoryFact

settings = get_settings()


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    key: str
    value: str
    confidence: float
    source_thread_id: str
    expires_at: datetime | None
    updated_at: datetime


def normalize_memory_key(key: str) -> str:
    """只接受白名单键，避免把表当成任意 KV 存储。"""
    normalized = key.strip().lower()
    if normalized not in MEMORY_VALUES:
        raise ValueError("不支持的记忆键")
    return normalized


def _utc_now_naive() -> datetime:
    """DATETIME 列的统一时钟基：不带时区、语义为 UTC。"""
    return datetime.now(UTC).replace(tzinfo=None)


def _active_clause():
    # 写入端和过滤端必须用同一个时钟。MySQL 的 func.now() 取 @@session.time_zone，
    # 而它由部署决定（本项目当前容器返回 +08），用它比较本应用写出的 UTC 时间会让 TTL
    # 随部署漂移，所以有效期判断只在应用时钟基内自洽。
    return or_(AgentMemoryFact.expires_at.is_(None), AgentMemoryFact.expires_at > _utc_now_naive())


async def recall_memories(owner: str) -> list[MemoryFact]:
    """按主体召回有效偏好；数据库异常由 Runtime 捕获并降级为空。"""
    async with auth_session_factory() as session:
        rows = (
            await session.execute(
                select(AgentMemoryFact)
                .where(AgentMemoryFact.owner == owner, _active_clause())
                .order_by(AgentMemoryFact.updated_at.desc())
                .limit(settings.agent_memory_max_facts)
            )
        ).scalars().all()
    return [
        MemoryFact(
            key=row.memory_key,
            value=row.memory_value,
            confidence=float(row.confidence),
        )
        for row in rows
    ]


async def remember_turn(turn: MemoryTurn) -> int:
    """从显式偏好语句中提取并 upsert；没有候选时不产生数据库写入。"""
    facts = extract_memories(turn.question)
    if not facts:
        return 0

    # expires_at 与 _active_clause() 同一时钟基；混用 func.now() 会让 TTL 随部署时区偏移。
    expires_at = (
        _utc_now_naive() + timedelta(days=settings.agent_memory_ttl_days)
        if settings.agent_memory_ttl_days > 0
        else None
    )
    async with auth_session_factory() as session:
        for fact in facts:
            statement = insert(AgentMemoryFact).values(
                owner=turn.owner,
                memory_key=fact.key,
                memory_value=fact.value,
                confidence=fact.confidence,
                source_thread_id=turn.thread_id[:128],
                source_request_id=turn.request_id[:64],
                expires_at=expires_at,
            )
            statement = statement.on_duplicate_key_update(
                memory_value=statement.inserted.memory_value,
                confidence=statement.inserted.confidence,
                source_thread_id=statement.inserted.source_thread_id,
                source_request_id=statement.inserted.source_request_id,
                expires_at=statement.inserted.expires_at,
                updated_at=func.now(),
            )
            await session.execute(statement)
        await session.commit()
    return len(facts)


async def list_memories(owner: str) -> list[MemoryRecord]:
    """返回当前主体仍有效的偏好，供用户查看与删除。"""
    async with auth_session_factory() as session:
        rows = (
            await session.execute(
                select(AgentMemoryFact)
                .where(AgentMemoryFact.owner == owner, _active_clause())
                .order_by(AgentMemoryFact.updated_at.desc())
                .limit(settings.agent_memory_max_facts)
            )
        ).scalars().all()
    return [
        MemoryRecord(
            key=row.memory_key,
            value=row.memory_value,
            confidence=float(row.confidence),
            source_thread_id=row.source_thread_id,
            expires_at=row.expires_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


async def delete_memory(owner: str, key: str) -> bool:
    """硬删除一条偏好；owner 过滤保证不能删除其他主体的记录。"""
    normalized = normalize_memory_key(key)
    async with auth_session_factory() as session:
        result = await session.execute(
            delete(AgentMemoryFact).where(
                AgentMemoryFact.owner == owner,
                AgentMemoryFact.memory_key == normalized,
            )
        )
        await session.commit()
    return bool(result.rowcount)
