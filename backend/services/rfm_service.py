import asyncio
import logging
import math
import time
from collections import OrderedDict
from datetime import UTC, datetime

from sqlalchemy import and_, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database_models import Order
from backend.utils.cache import cached
from backend.utils.rfm_scoring import assign_segment, quantile_scores

logger = logging.getLogger(__name__)


class RfmDataUnavailableError(RuntimeError):
    """数据库暂无可用订单数据，RFM 无法计算。

    属于数据未就绪（503 语义）而非服务器 bug；必须以异常抛出，
    避免错误结果被 @cached / 快照缓存固化 10 分钟。
    """

VALID_SEGMENTS = [
    "重要价值客户", "重要发展客户", "重要保持客户", "重要挽留客户",
    "一般价值客户", "一般发展客户", "一般保持客户", "一般挽留客户",
]

_SEGMENT_PRIORITY = {
    "重要价值客户": 1, "重要发展客户": 2, "重要保持客户": 3, "重要挽留客户": 4,
    "一般价值客户": 5, "一般发展客户": 6, "一般保持客户": 7, "一般挽留客户": 8,
}

# 完整用户明细只缓存在当前进程，避免把 7 万+ 用户序列化为超大 Redis Key。
# Docker 当前使用单 Worker；若未来扩容为多 Worker，各进程会各自维护一份有界快照。
_SNAPSHOT_TTL_SECONDS = 600
_SNAPSHOT_MAX_ENTRIES = 8
_snapshot_cache: OrderedDict[tuple[str | None, int], dict] = OrderedDict()
_snapshot_locks: dict[tuple[str | None, int], asyncio.Lock] = {}
# 与 backend/utils/cache.py 同理：锁可能"已取用但还在等待队列中"，
# 用引用计数保护，防止快照逐出时误回收正在使用的锁
_snapshot_lock_refs: dict[tuple[str | None, int], int] = {}


async def _fetch_rfm_raw(db: AsyncSession, ref_date) -> list[dict]:
    rfm_stmt = select(
        Order.user_name,
        func.datediff(literal(ref_date), func.max(Order.order_date)).label("recency_days"),
        # F 口径 = 去重订单数：与 BI 看板 nunique('订单号') 保持一致，
        # 若同一订单号存在多行，count(id) 会把 F 值算高、分群结果前后端不一致
        func.count(func.distinct(Order.order_no)).label("frequency"),
        func.coalesce(func.sum(Order.payment_amount), 0).label("monetary"),
    ).where(
        and_(
            Order.order_date <= ref_date,
            Order.payment_amount > 0,
        )
    ).group_by(
        Order.user_name
    )
    result = await db.execute(rfm_stmt)
    rows = result.all()
    return [
        {
            "user_name": r.user_name,
            "recency_days": r.recency_days,
            "frequency": r.frequency,
            "monetary": round(float(r.monetary), 2),
        }
        for r in rows
    ]


def _score_users(users: list[dict], n_bins: int = 5) -> list[dict]:
    if not users:
        return users

    recency_scores = quantile_scores(
        [u["recency_days"] for u in users],
        n_bins,
    )
    frequency_scores = quantile_scores(
        [u["frequency"] for u in users],
        n_bins,
    )
    monetary_scores = quantile_scores(
        [u["monetary"] for u in users],
        n_bins,
    )

    threshold = max(2, math.ceil(n_bins * 0.6))
    for u, r_score, f_score, m_score in zip(
        users,
        recency_scores,
        frequency_scores,
        monetary_scores,
    ):
        u["r_score"] = r_score
        u["f_score"] = f_score
        u["m_score"] = m_score
        u["rfm_score"] = f"{u['r_score']}{u['f_score']}{u['m_score']}"
        u["segment"] = assign_segment(
            u["r_score"],
            u["f_score"],
            u["m_score"],
            threshold,
            threshold,
            threshold,
        )

    return users


def _build_segments(users: list[dict]) -> list[dict]:
    segment_counts: dict[str, int] = {}
    segment_monetary: dict[str, float] = {}
    for u in users:
        seg = u["segment"]
        segment_counts[seg] = segment_counts.get(seg, 0) + 1
        segment_monetary[seg] = segment_monetary.get(seg, 0) + u["monetary"]

    total_users = len(users)
    segments = []
    for seg in sorted(segment_counts.keys(), key=lambda s: _SEGMENT_PRIORITY.get(s, 9)):
        count = segment_counts[seg]
        segments.append({
            "segment": seg,
            "count": count,
            "percentage": round(count / total_users * 100, 2) if total_users > 0 else 0,
            "avg_monetary": round(segment_monetary[seg] / count, 2),
            "total_monetary": round(segment_monetary[seg], 2),
        })
    return segments


async def _build_rfm_snapshot(
    db: AsyncSession,
    reference_date: str | None = None,
    n_bins: int = 5,
) -> dict:
    """计算一份完整 RFM 快照，供汇总、分页和 TOP 用户共同复用。"""
    if reference_date:
        ref_date = datetime.strptime(reference_date, "%Y-%m-%d").replace(tzinfo=UTC).date()
    else:
        max_date_stmt = select(func.max(Order.order_date))
        result = await db.execute(max_date_stmt)
        ref_date = result.scalar()
        if ref_date is None:
            raise RfmDataUnavailableError("数据库中无订单数据")

    users = await _fetch_rfm_raw(db, ref_date)
    if not users:
        raise RfmDataUnavailableError("无有效付款订单数据")

    users = _score_users(users, n_bins)
    segments = _build_segments(users)

    total_users = len(users)
    avg_recency = round(sum(u["recency_days"] for u in users) / total_users, 1)
    avg_frequency = round(sum(u["frequency"] for u in users) / total_users, 2)
    avg_monetary = round(sum(u["monetary"] for u in users) / total_users, 2)

    sorted_users = sorted(
        users,
        key=lambda u: (
            u["r_score"],
            -u["f_score"],
            -u["m_score"],
            -u["monetary"],
        ),
    )

    return {
        "reference_date": str(ref_date),
        "total_users": total_users,
        "n_bins": n_bins,
        "score_threshold": max(2, math.ceil(n_bins * 0.6)),
        "averages": {
            "recency_days": avg_recency,
            "frequency": avg_frequency,
            "monetary": avg_monetary,
        },
        "segments": segments,
        "users": users,
        "sorted_users": sorted_users,
    }


async def _get_rfm_snapshot(
    db: AsyncSession,
    reference_date: str | None = None,
    n_bins: int = 5,
) -> dict:
    """获取有界、带 TTL 的进程内快照，防止并发请求重复执行重计算。"""
    key = (reference_date, n_bins)
    now = time.monotonic()
    cached_snapshot = _snapshot_cache.get(key)
    if cached_snapshot and cached_snapshot["expires_at"] > now:
        _snapshot_cache.move_to_end(key)
        return cached_snapshot["data"]

    lock = _snapshot_locks.setdefault(key, asyncio.Lock())
    _snapshot_lock_refs[key] = _snapshot_lock_refs.get(key, 0) + 1
    try:
        async with lock:
            now = time.monotonic()
            cached_snapshot = _snapshot_cache.get(key)
            if cached_snapshot and cached_snapshot["expires_at"] > now:
                _snapshot_cache.move_to_end(key)
                return cached_snapshot["data"]

            snapshot = await _build_rfm_snapshot(
                db,
                reference_date=reference_date,
                n_bins=n_bins,
            )
            _snapshot_cache[key] = {
                "data": snapshot,
                "expires_at": now + _SNAPSHOT_TTL_SECONDS,
            }
            _snapshot_cache.move_to_end(key)

            while len(_snapshot_cache) > _SNAPSHOT_MAX_ENTRIES:
                _snapshot_cache.popitem(last=False)
            # 只回收无人取用且未持有的锁，防止高基数 reference_date 场景锁字典泄漏
            for stale_key in [
                k for k, lk in _snapshot_locks.items()
                if k not in _snapshot_cache
                and _snapshot_lock_refs.get(k, 0) == 0
                and not lk.locked()
            ]:
                _snapshot_locks.pop(stale_key, None)
                _snapshot_lock_refs.pop(stale_key, None)
            return snapshot
    finally:
        _snapshot_lock_refs[key] = _snapshot_lock_refs.get(key, 1) - 1


def clear_rfm_snapshot_cache() -> None:
    """清空 RFM 进程内快照；数据源变更后调用。"""
    _snapshot_cache.clear()
    _snapshot_locks.clear()
    _snapshot_lock_refs.clear()


def _summary_from_snapshot(snapshot: dict) -> dict:
    """提取适合 Redis 缓存和 API 返回的小型汇总对象。"""
    return {
        "reference_date": snapshot["reference_date"],
        "total_users": snapshot["total_users"],
        "n_bins": snapshot["n_bins"],
        "score_threshold": snapshot["score_threshold"],
        "averages": snapshot["averages"],
        "segments": snapshot["segments"],
    }


@cached(ttl=600)
async def compute_rfm(
    db: AsyncSession,
    reference_date: str | None = None,
    n_bins: int = 5,
) -> dict:
    """返回 RFM 汇总；Redis 中不再保存完整用户明细。"""
    snapshot = await _get_rfm_snapshot(
        db,
        reference_date=reference_date,
        n_bins=n_bins,
    )
    return _summary_from_snapshot(snapshot)


@cached(ttl=300)
async def get_rfm_segment_detail(
    db: AsyncSession,
    segment: str,
    page: int = 1,
    page_size: int = 20,
    reference_date: str | None = None,
    n_bins: int = 5,
) -> dict:
    snapshot = await _get_rfm_snapshot(
        db,
        reference_date=reference_date,
        n_bins=n_bins,
    )
    users = snapshot.get("users", [])

    filtered = [u for u in users if u["segment"] == segment]

    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    page_users = filtered[start:end]

    return {
        "segment": segment,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "users": page_users,
    }


@cached(ttl=300)
async def get_rfm_top_users(
    db: AsyncSession,
    limit: int = 20,
    reference_date: str | None = None,
    n_bins: int = 5,
) -> dict:
    """按 RFM 价值顺序返回指定数量用户，正确支持 1-100 条。"""
    snapshot = await _get_rfm_snapshot(
        db,
        reference_date=reference_date,
        n_bins=n_bins,
    )
    return {
        "reference_date": snapshot["reference_date"],
        "total_users": snapshot["total_users"],
        "top_users": snapshot["sorted_users"][:limit],
    }


@cached(ttl=600)
async def get_rfm_overview(db: AsyncSession) -> dict:
    rfm_data = await compute_rfm(db)

    segments = rfm_data["segments"]
    segment_distribution = [{"segment": s["segment"], "count": s["count"], "percentage": s["percentage"]} for s in segments]
    monetary_distribution = [{"segment": s["segment"], "total_monetary": s["total_monetary"], "avg_monetary": s["avg_monetary"]} for s in segments]

    high_value = [s for s in segments if "重要" in s["segment"]]
    high_value_count = sum(s["count"] for s in high_value)
    high_value_monetary = sum(s["total_monetary"] for s in high_value)

    at_risk = [s for s in segments if "挽留" in s["segment"]]
    at_risk_count = sum(s["count"] for s in at_risk)

    return {
        "reference_date": rfm_data["reference_date"],
        "total_users": rfm_data["total_users"],
        "averages": rfm_data["averages"],
        "high_value_users": {
            "count": high_value_count,
            "percentage": round(high_value_count / rfm_data["total_users"] * 100, 2),
            "total_monetary": round(high_value_monetary, 2),
        },
        "at_risk_users": {
            "count": at_risk_count,
            "percentage": round(at_risk_count / rfm_data["total_users"] * 100, 2),
        },
        "segment_distribution": segment_distribution,
        "monetary_distribution": monetary_distribution,
    }
