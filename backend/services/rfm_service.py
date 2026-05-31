import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func, and_, literal
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database_models import Order
from backend.utils.cache import cached

logger = logging.getLogger(__name__)

VALID_SEGMENTS = [
    "重要价值客户", "重要发展客户", "重要保持客户", "重要挽留客户",
    "一般价值客户", "一般发展客户", "一般保持客户", "一般挽留客户",
]

_SEGMENT_PRIORITY = {
    "重要价值客户": 1, "重要发展客户": 2, "重要保持客户": 3, "重要挽留客户": 4,
    "一般价值客户": 5, "一般发展客户": 6, "一般保持客户": 7, "一般挽留客户": 8,
}


def _rfm_score(r_val: int, f_val: int, m_val: int) -> str:
    if r_val >= 4 and f_val >= 4 and m_val >= 4:
        return "重要价值客户"
    if r_val >= 4 and f_val >= 4 and m_val < 4:
        return "重要发展客户"
    if r_val >= 4 and f_val < 4 and m_val >= 4:
        return "重要保持客户"
    if r_val >= 4 and f_val < 4 and m_val < 4:
        return "重要挽留客户"
    if r_val < 4 and f_val >= 4 and m_val >= 4:
        return "一般价值客户"
    if r_val < 4 and f_val >= 4 and m_val < 4:
        return "一般发展客户"
    if r_val < 4 and f_val < 4 and m_val >= 4:
        return "一般保持客户"
    return "一般挽留客户"


def _quantile_score(values: list, val: float, n_bins: int = 5, reverse: bool = False) -> int:
    if not values:
        return 1
    n = len(values)
    if reverse:
        # Recency: 低值=高分（最近消费），从高分往低分匹配
        for i in range(n_bins, 0, -1):
            threshold_idx = int(n * (n_bins - i + 1) / n_bins) - 1
            threshold_idx = max(0, min(threshold_idx, n - 1))
            if val <= values[threshold_idx]:
                return i
    else:
        # Frequency/Monetary: 高值=高分（高频/高消费），从低分往高分匹配
        for i in range(1, n_bins + 1):
            threshold_idx = int(n * i / n_bins) - 1
            threshold_idx = max(0, min(threshold_idx, n - 1))
            if val <= values[threshold_idx]:
                return i
    return n_bins


async def _fetch_rfm_raw(db: AsyncSession, ref_date) -> list[dict]:
    rfm_stmt = select(
        Order.user_name,
        func.datediff(literal(ref_date), func.max(Order.order_date)).label("recency_days"),
        func.count(Order.id).label("frequency"),
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

    recency_values = sorted(u["recency_days"] for u in users)
    frequency_values = sorted(u["frequency"] for u in users)
    monetary_values = sorted(u["monetary"] for u in users)

    for u in users:
        u["r_score"] = _quantile_score(recency_values, u["recency_days"], n_bins, reverse=True)
        u["f_score"] = _quantile_score(frequency_values, u["frequency"], n_bins, reverse=False)
        u["m_score"] = _quantile_score(monetary_values, u["monetary"], n_bins, reverse=False)
        u["rfm_score"] = f"{u['r_score']}{u['f_score']}{u['m_score']}"
        u["segment"] = _rfm_score(u["r_score"], u["f_score"], u["m_score"])

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


@cached(ttl=600)
async def compute_rfm(
    db: AsyncSession,
    reference_date: Optional[str] = None,
    n_bins: int = 5,
) -> dict:
    if reference_date:
        ref_date = datetime.strptime(reference_date, "%Y-%m-%d").date()
    else:
        max_date_stmt = select(func.max(Order.order_date))
        result = await db.execute(max_date_stmt)
        ref_date = result.scalar()
        if ref_date is None:
            return {"error": "数据库中无订单数据", "total_users": 0}

    users = await _fetch_rfm_raw(db, ref_date)
    if not users:
        return {"error": "无有效付款订单数据", "total_users": 0}

    users = _score_users(users, n_bins)
    segments = _build_segments(users)

    total_users = len(users)
    avg_recency = round(sum(u["recency_days"] for u in users) / total_users, 1)
    avg_frequency = round(sum(u["frequency"] for u in users) / total_users, 2)
    avg_monetary = round(sum(u["monetary"] for u in users) / total_users, 2)

    sorted_users = sorted(users, key=lambda u: u["r_score"] * 100 + u["f_score"] * 10 + u["m_score"], reverse=True)

    return {
        "reference_date": str(ref_date),
        "total_users": total_users,
        "n_bins": n_bins,
        "averages": {
            "recency_days": avg_recency,
            "frequency": avg_frequency,
            "monetary": avg_monetary,
        },
        "segments": segments,
        "top_users": sorted_users[:20],
    }


@cached(ttl=300)
async def get_rfm_segment_detail(
    db: AsyncSession,
    segment: str,
    page: int = 1,
    page_size: int = 20,
    reference_date: Optional[str] = None,
    n_bins: int = 5,
) -> dict:
    # 优先复用 compute_rfm 的缓存结果
    rfm_data = await compute_rfm(db, reference_date=reference_date, n_bins=n_bins)
    if "error" in rfm_data:
        return rfm_data

    all_users = rfm_data.get("top_users", [])
    # top_users 仅包含前20名，需要全量数据时重新计算
    # 通过 segment 过滤需要完整用户列表，因此从 compute_rfm 获取
    # 但 compute_rfm 缓存中只有 top_users，需要独立计算完整列表
    if not all_users:
        return {"error": "无有效付款订单数据", "total_users": 0}

    # 如果缓存的 top_users 不够完整（仅20条），重新获取全量
    if rfm_data["total_users"] > 20:
        # 从数据库重新获取完整用户列表并评分
        if reference_date:
            ref_date = datetime.strptime(reference_date, "%Y-%m-%d").date()
        else:
            max_date_stmt = select(func.max(Order.order_date))
            result = await db.execute(max_date_stmt)
            ref_date = result.scalar()

        users = await _fetch_rfm_raw(db, ref_date)
        users = _score_users(users, n_bins)
    else:
        users = all_users

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


@cached(ttl=600)
async def get_rfm_overview(db: AsyncSession) -> dict:
    rfm_data = await compute_rfm(db)
    if "error" in rfm_data:
        return rfm_data

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
