"""历史情节：摘要落库、本地向量召回与用户管理。

向量模型不可用时自动降级为关键词重叠打分；所有查询强制带 owner。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.mysql import insert

from agent_core.memory import (
    EpisodeSummary,
    EpisodeTurn,
    build_episode_summary,
    keyword_score,
    needs_episode_recall,
)
from backend.config import get_settings
from backend.database import auth_session_factory
from backend.models.database_models import AgentMemoryEpisode
from backend.services.episode_embeddings import embed_texts

logger = logging.getLogger(__name__)
settings = get_settings()

_background_tasks: set[asyncio.Task] = set()
_EMBED_TIMEOUT_SECONDS = 3.0
_KEYWORD_MIN_SCORE = 0.20


@dataclass(frozen=True, slots=True)
class EpisodeRecord:
    id: int
    summary: str
    created_at: datetime
    expires_at: datetime | None
    has_vector: bool


def _active_clause():
    return or_(AgentMemoryEpisode.expires_at.is_(None), AgentMemoryEpisode.expires_at > func.now())


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


async def _embed_query(query: str) -> list[float] | None:
    try:
        vectors = await asyncio.wait_for(
            asyncio.to_thread(embed_texts, [query]),
            timeout=_EMBED_TIMEOUT_SECONDS,
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("情节查询向量生成超时，已降级为关键词")
        return None
    if not vectors:
        return None
    vector = vectors[0]
    return vector if vector else None


def _schedule_embedding(owner: str, request_id: str, summary: str) -> None:
    """摘要先落库，向量后台补写；避免模型首次加载阻塞回答。"""
    if not settings.agent_episode_vector_enabled:
        return
    try:
        task = asyncio.create_task(_embed_episode(owner, request_id, summary))
    except RuntimeError:
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _embed_episode(owner: str, request_id: str, summary: str) -> None:
    try:
        vectors = await asyncio.to_thread(embed_texts, [summary])
        if not vectors or not vectors[0]:
            return
        async with auth_session_factory() as session:
            await session.execute(
                update(AgentMemoryEpisode)
                .where(
                    AgentMemoryEpisode.owner == owner,
                    AgentMemoryEpisode.source_request_id == request_id,
                )
                .values(embedding=vectors[0], embedding_model=settings.agent_episode_embedding_model)
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - 向量补写失败只影响召回质量
        logger.warning("情节向量补写失败: %s", type(exc).__name__)


async def save_episode(turn: EpisodeTurn) -> bool:
    """保存压缩后的单轮情节；clarification/blocked 不进入长期情节。"""
    if not settings.agent_episode_enabled or turn.intent in {"blocked", "clarification"}:
        return False
    summary = build_episode_summary(turn)
    if not summary:
        return False

    expires_at = (
        datetime.now() + timedelta(days=settings.agent_episode_ttl_days)
        if settings.agent_episode_ttl_days > 0
        else None
    )
    async with auth_session_factory() as session:
        statement = insert(AgentMemoryEpisode).values(
            owner=turn.owner,
            thread_id=turn.thread_id[:128],
            source_request_id=turn.request_id[:64],
            summary=summary,
            intent=turn.intent,
            embedding=None,
            embedding_model="",
            expires_at=expires_at,
        )
        statement = statement.on_duplicate_key_update(
            thread_id=statement.inserted.thread_id,
            summary=statement.inserted.summary,
            intent=statement.inserted.intent,
            embedding=None,
            embedding_model="",
            expires_at=statement.inserted.expires_at,
        )
        await session.execute(statement)
        # 惰性清理过期情节，并把主体保留量压到配置上限。
        await session.execute(
            delete(AgentMemoryEpisode).where(
                AgentMemoryEpisode.owner == turn.owner,
                AgentMemoryEpisode.expires_at.is_not(None),
                AgentMemoryEpisode.expires_at <= func.now(),
            )
        )
        keep_ids = (
            await session.execute(
                select(AgentMemoryEpisode.id)
                .where(AgentMemoryEpisode.owner == turn.owner)
                .order_by(AgentMemoryEpisode.created_at.desc(), AgentMemoryEpisode.id.desc())
                .limit(settings.agent_episode_max_per_owner)
            )
        ).scalars().all()
        if keep_ids:
            await session.execute(
                delete(AgentMemoryEpisode).where(
                    AgentMemoryEpisode.owner == turn.owner,
                    AgentMemoryEpisode.id.not_in(keep_ids),
                )
            )
        await session.commit()

    _schedule_embedding(turn.owner, turn.request_id[:64], summary)
    return True


async def recall_episodes(owner: str, query: str) -> list[EpisodeSummary]:
    """仅在用户回指历史时召回；向量失败时自动使用关键词打分。"""
    if not settings.agent_episode_enabled or not needs_episode_recall(query):
        return []
    async with auth_session_factory() as session:
        rows = (
            await session.execute(
                select(AgentMemoryEpisode)
                .where(AgentMemoryEpisode.owner == owner, _active_clause())
                .order_by(AgentMemoryEpisode.created_at.desc(), AgentMemoryEpisode.id.desc())
                .limit(settings.agent_episode_max_per_owner)
            )
        ).scalars().all()
    if not rows:
        return []

    ranked: list[tuple[float, AgentMemoryEpisode]] = []
    query_vector = await _embed_query(query) if settings.agent_episode_vector_enabled else None
    if query_vector:
        for row in rows:
            embedding = row.embedding if isinstance(row.embedding, list) else None
            if not embedding:
                continue
            score = _cosine(query_vector, [float(value) for value in embedding])
            if score >= settings.agent_episode_min_score:
                ranked.append((score, row))
    if not ranked:
        for row in rows:
            score = keyword_score(query, row.summary)
            if score >= _KEYWORD_MIN_SCORE:
                ranked.append((score, row))

    ranked.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
    return [
        EpisodeSummary(
            episode_id=row.id,
            summary=row.summary,
            score=round(score, 4),
            created_at=row.created_at.strftime("%Y-%m-%d"),
        )
        for score, row in ranked[: settings.agent_episode_top_k]
    ]


async def list_episodes(owner: str) -> list[EpisodeRecord]:
    async with auth_session_factory() as session:
        rows = (
            await session.execute(
                select(AgentMemoryEpisode)
                .where(AgentMemoryEpisode.owner == owner, _active_clause())
                .order_by(AgentMemoryEpisode.created_at.desc(), AgentMemoryEpisode.id.desc())
                .limit(settings.agent_episode_max_per_owner)
            )
        ).scalars().all()
    return [
        EpisodeRecord(
            id=row.id,
            summary=row.summary,
            created_at=row.created_at,
            expires_at=row.expires_at,
            has_vector=bool(row.embedding),
        )
        for row in rows
    ]


async def delete_episode(owner: str, episode_id: int) -> bool:
    async with auth_session_factory() as session:
        result = await session.execute(
            delete(AgentMemoryEpisode).where(
                AgentMemoryEpisode.owner == owner,
                AgentMemoryEpisode.id == episode_id,
            )
        )
        await session.commit()
    return bool(result.rowcount)
