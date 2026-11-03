import asyncio
import logging
import math
import time
from collections import OrderedDict
from datetime import datetime

from sqlalchemy import and_, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database_models import Order
# 复用缓存层的版本读取：快照键与 @cached(versioned=True) 必须使用同一个
# data_version 来源与同一套"读不到就回退 TTL"的语义，否则两层各自判空会打架。
from backend.utils.cache import _read_session_data_version, cached
from backend.utils.rfm_scoring import (
    AT_RISK_SEGMENTS,
    HIGH_VALUE_SEGMENTS,
    assign_segment,
    quantile_scores,
)

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

# 高价值/流失预警的分群归组与分位评分同源于 backend/utils/rfm_scoring，
# BI 看板与本服务共用同一组常量，不允许各写一份口径。

# 完整用户明细只缓存在当前进程，避免把 7 万+ 用户序列化为超大 Redis Key。
# Docker 当前使用单 Worker；若未来扩容为多 Worker，各进程会各自维护一份有界快照。
_SNAPSHOT_TTL_SECONDS = 600
_SNAPSHOT_MAX_ENTRIES = 8
# 键含 data_version：事件摄入/导入 CLI 提交后版本递增，陈旧快照不可能再被
# 写入新版本键，从而击穿 @cached(versioned=True) 的版本化保证。
_SnapshotKey = tuple[str | None, int, int | None]
_snapshot_cache: OrderedDict[_SnapshotKey, dict] = OrderedDict()


class _SnapshotLock:
    """与 backend/utils/cache.py 的 _RefLock 同理：refs 覆盖"已取用但仍在排队"的全周期。

    仅用 lock.locked() 判断空闲会误回收等待中的锁，导致 single-flight 失效；
    refs 挂在锁对象上而不是平行的字典，也就不会出现"清空字典后 finally 又把键写回去"
    造成的无界增长。
    """

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


_snapshot_locks: dict[_SnapshotKey, _SnapshotLock] = {}


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
            Order.user_name.is_not(None),
            Order.order_date <= ref_date,
            Order.payment_amount > 0,
            # 与 BI/AI 统一：取消订单不参与 RFM；退款订单保留，M 值为历史付款额。
            Order.order_status != "CANCELLED",
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
        strict=True,
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
        # 业务日期是墙钟日期：解析成 date 后不附加任何时区。
        ref_date = datetime.strptime(reference_date, "%Y-%m-%d").date()
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
    """获取有界、带 TTL 且受数据版本约束的进程内快照，防止并发请求重复执行重计算。"""
    # 读版本必须发生在业务查询之前，与快照共享同一 REPEATABLE READ 语义；
    # 读不到时返回 None，快照退化为纯 TTL（与 @cached(versioned=True) 一致）。
    key = (reference_date, n_bins, await _read_session_data_version(db))
    now = time.monotonic()
    cached_snapshot = _snapshot_cache.get(key)
    if cached_snapshot and cached_snapshot["expires_at"] > now:
        _snapshot_cache.move_to_end(key)
        return cached_snapshot["data"]

    entry = _snapshot_locks.setdefault(key, _SnapshotLock())
    # setdefault 与计数之间不能有 await：否则别的协程可能已把我们正在等待的
    # 锁当成空闲对象回收掉。
    entry.refs += 1
    try:
        async with entry.lock:
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
            _reclaim_idle_snapshot_locks()
            return snapshot
    finally:
        entry.refs -= 1
        # 只有最后一个持有者才回收：否则会把仍在重计算的锁从字典摘掉，
        # 让后续请求新建一把锁并行重算，single-flight 失效。
        if entry.refs == 0 and _snapshot_locks.get(key) is entry:
            _snapshot_locks.pop(key, None)


def _reclaim_idle_snapshot_locks() -> None:
    """回收既不在快照缓存中、也无人取用的锁，防止高基数 reference_date 造成锁字典泄漏。"""
    for stale_key in [
        k for k, entry in _snapshot_locks.items()
        if k not in _snapshot_cache and entry.refs == 0 and not entry.lock.locked()
    ]:
        _snapshot_locks.pop(stale_key, None)


def clear_rfm_snapshot_cache() -> None:
    """清空 RFM 进程内快照；数据源变更后调用。

    只清数据、不整表清锁：order_event_service 在请求处理过程中就会调用本函数，
    此时可能有协程正持有快照锁，连带清空锁字典会让它们各跑各的重计算。
    空闲锁由下一次重建时的回收路径处理。
    """
    _snapshot_cache.clear()
    _reclaim_idle_snapshot_locks()


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


@cached(ttl=600, tags=("rfm",), versioned=True)
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


@cached(ttl=300, tags=("rfm",), versioned=True)
async def get_rfm_segment_detail(
    db: AsyncSession,
    segment: str,
    page: int = 1,
    page_size: int = 20,
    reference_date: str | None = None,
    n_bins: int = 5,
    r_threshold: int | None = None,
    f_threshold: int | None = None,
    m_threshold: int | None = None,
) -> dict:
    snapshot = await _get_rfm_snapshot(
        db,
        reference_date=reference_date,
        n_bins=n_bins,
    )
    users = snapshot.get("users", [])

    # 明细与导出使用同一套阈值重算逻辑，避免"同名分群"在不同入口指向不同人群。
    default_threshold = max(2, math.ceil(n_bins * 0.6))
    thresholds = (
        r_threshold or default_threshold,
        f_threshold or default_threshold,
        m_threshold or default_threshold,
    )
    filtered = [
        u for u in users
        if assign_segment(
            u["r_score"], u["f_score"], u["m_score"], *thresholds
        ) == segment
    ]

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


async def get_rfm_segment_export_rows(
    db: AsyncSession,
    segment: str,
    reference_date: str | None = None,
    n_bins: int = 5,
    r_threshold: int | None = None,
    f_threshold: int | None = None,
    m_threshold: int | None = None,
    limit: int = 200,
) -> list[dict]:
    """按 BI 页面当前阈值重新分层并返回导出明细。

    快照里的 segment 使用固定阈值缓存，不能直接复用；这里只在内存中重新
    计算分层，避免为不同阈值重复扫描数据库。
    """
    default_threshold = max(2, math.ceil(n_bins * 0.6))
    thresholds = (
        r_threshold or default_threshold,
        f_threshold or default_threshold,
        m_threshold or default_threshold,
    )
    snapshot = await _get_rfm_snapshot(
        db,
        reference_date=reference_date,
        n_bins=n_bins,
    )
    rows = []
    for user in snapshot["users"]:
        assigned = assign_segment(
            user["r_score"],
            user["f_score"],
            user["m_score"],
            *thresholds,
        )
        if assigned != segment:
            continue
        rows.append({
            "user_name": user["user_name"],
            "r_score": user["r_score"],
            "f_score": user["f_score"],
            "m_score": user["m_score"],
            "recency_days": user["recency_days"],
            "frequency": user["frequency"],
            "monetary": user["monetary"],
        })
    rows.sort(key=lambda item: item["monetary"], reverse=True)
    return rows[:limit]


@cached(ttl=300, tags=("rfm",), versioned=True)
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


@cached(ttl=600, tags=("rfm",), versioned=True)
async def get_rfm_overview(
    db: AsyncSession,
    reference_date: str | None = None,
    n_bins: int = 5,
) -> dict:
    rfm_data = await compute_rfm(db, reference_date=reference_date, n_bins=n_bins)

    segments = rfm_data["segments"]
    segment_distribution = [{"segment": s["segment"], "count": s["count"], "percentage": s["percentage"]} for s in segments]
    monetary_distribution = [{"segment": s["segment"], "total_monetary": s["total_monetary"], "avg_monetary": s["avg_monetary"]} for s in segments]

    high_value = [s for s in segments if s["segment"] in HIGH_VALUE_SEGMENTS]
    high_value_count = sum(s["count"] for s in high_value)
    high_value_monetary = sum(s["total_monetary"] for s in high_value)

    at_risk = [s for s in segments if s["segment"] in AT_RISK_SEGMENTS]
    at_risk_count = sum(s["count"] for s in at_risk)

    total_users = rfm_data["total_users"] or 1

    return {
        "reference_date": rfm_data["reference_date"],
        "total_users": rfm_data["total_users"],
        "n_bins": rfm_data["n_bins"],
        "averages": rfm_data["averages"],
        "high_value_users": {
            "count": high_value_count,
            "percentage": round(high_value_count / total_users * 100, 2),
            "total_monetary": round(high_value_monetary, 2),
        },
        "at_risk_users": {
            "count": at_risk_count,
            "percentage": round(at_risk_count / total_users * 100, 2),
        },
        "segment_distribution": segment_distribution,
        "monetary_distribution": monetary_distribution,
    }
