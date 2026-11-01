"""AI 后端的成本与容量边界：每日额度、全局并发、单用户并发与用量审计。

边界只在后端执行。Streamlit 只是 UI，不能自行决定额度或并发；服务凭据与
人类用户共用同一套主体名称键。单 uvicorn worker 部署下这些计数是进程内
准确的；多 worker 时并发上限会按 worker 放大，需在部署文档中显式说明。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import weakref
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.models.database_models import AIQueryAudit
from backend.utils.text_cleaner import sanitize_error

logger = logging.getLogger(__name__)
settings = get_settings()


class AILimitError(RuntimeError):
    code = "AI_LIMIT_EXCEEDED"
    status_code = 429

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AIDailyQuotaExceeded(AILimitError):
    code = "AI_DAILY_QUOTA_EXCEEDED"


class AIInflightExceeded(AILimitError):
    code = "AI_INFLIGHT_LIMIT"


class AIQueueTimeout(AILimitError):
    code = "AI_QUEUE_TIMEOUT"
    status_code = 503


# 事件循环退出后自动回收信号量，避免 pytest 多 loop 环境下跨 loop 复用。
_semaphores: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_user_inflight: dict[str, int] = {}


def _global_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(settings.ai_max_concurrency)
        _semaphores[loop] = semaphore
    return semaphore


def _utc_day_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


async def daily_quota_used(session: AsyncSession, principal_name: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(AIQueryAudit)
            .where(
                AIQueryAudit.principal_name == principal_name,
                AIQueryAudit.created_at >= _utc_day_start(),
                AIQueryAudit.outcome != "denied",
            )
        )
        or 0
    )


async def ensure_quota(session: AsyncSession, principal_name: str) -> None:
    used = await daily_quota_used(session, principal_name)
    if used >= settings.ai_daily_request_limit:
        raise AIDailyQuotaExceeded(
            f"今日 AI 查询额度已用完（{settings.ai_daily_request_limit} 次），请明日再试"
        )


def _enter_inflight(principal_name: str) -> None:
    current = _user_inflight.get(principal_name, 0)
    if current >= settings.ai_max_inflight_per_user:
        raise AIInflightExceeded("当前账号已有 AI 查询在运行，请等待完成后再试")
    # 单事件循环内没有 await，读改写是原子的。
    _user_inflight[principal_name] = current + 1


def _leave_inflight(principal_name: str) -> None:
    current = _user_inflight.get(principal_name, 0)
    if current <= 1:
        _user_inflight.pop(principal_name, None)
    else:
        _user_inflight[principal_name] = current - 1


@asynccontextmanager
async def ai_execution_slot(principal_name: str, auth_db: AsyncSession):
    """额度 → 主体并发 → 全局并发 → 执行；任一层拒绝都返回明确错误。"""
    slot = await acquire_ai_slot(principal_name, auth_db)
    try:
        yield slot
    finally:
        slot.release()


class AISlot:
    """显式持有/释放的槽位；流式响应必须在生成器结束时释放。"""

    def __init__(self, principal_name: str, semaphore: asyncio.Semaphore):
        self.principal_name = principal_name
        self._semaphore = semaphore
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._semaphore.release()
        _leave_inflight(self.principal_name)


async def acquire_ai_slot(principal_name: str, auth_db: AsyncSession) -> AISlot:
    await ensure_quota(auth_db, principal_name)
    _enter_inflight(principal_name)
    semaphore = _global_semaphore()
    try:
        await asyncio.wait_for(
            semaphore.acquire(),
            timeout=settings.ai_queue_timeout_seconds,
        )
    except (TimeoutError, asyncio.TimeoutError) as exc:
        _leave_inflight(principal_name)
        raise AIQueueTimeout("AI 服务繁忙，请稍后重试") from exc
    except BaseException:
        _leave_inflight(principal_name)
        raise
    return AISlot(principal_name, semaphore)


def query_sha256(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


async def audit_ai_query(
    session: AsyncSession,
    *,
    request_id: str,
    principal_name: str,
    thread_id: str,
    model_mode: str,
    query: str,
    outcome: str,
    intent: str = "",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    latency_ms: int | None = None,
    error_category: str = "",
) -> None:
    """只记录哈希、长度与用量；原始问题、密钥与回答正文都不落审计表。"""
    try:
        session.add(
            AIQueryAudit(
                request_id=request_id[:64],
                principal_name=principal_name[:100],
                thread_id=thread_id[:128],
                model_mode=model_mode[:20],
                intent=intent[:32],
                outcome=outcome[:20],
                query_sha256=query_sha256(query),
                query_length=len(query),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_ms=latency_ms,
                error_category=error_category[:80],
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - 审计失败不改变已回答的事实
        await session.rollback()
        logger.warning("AI 审计写入失败: %s", sanitize_error(exc))


def usage_fields(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "latency_ms": None,
        }
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "latency_ms": usage.latency_ms,
    }
