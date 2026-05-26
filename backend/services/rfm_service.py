import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func, case, text, and_, literal
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database_models import Order
from backend.utils.cache import cached

logger = logging.getLogger(__name__)


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


def _rfm_segment_priority(segment: str) -> int:
    priorities = {
        "重要价值客户": 1, "重要发展客户": 2, "重要保持客户": 3, "重要挽留客户": 4,
        "一般价值客户": 5, "一般发展客户": 6, "一般保持客户": 7, "一般挽留客户": 8,
    }
    return priorities.get(segment, 9)


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

    if not rows:
        return {"error": "无有效付款订单数据", "total_users": 0}

    users = []
    for row in rows:
        users.append({
            "user_name": row.user_name,
            "recency_days": row.recency_days,
            "frequency": row.frequency,
            "monetary": round(float(row.monetary), 2),
        })

    recency_values = sorted([u["recency_days"] for u in users])
    frequency_values = sorted([u["frequency"] for u in users])
    monetary_values = sorted([u["monetary"] for u in users])

    def _quantile_score(values: list, val: float, reverse: bool = False) -> int:
        if not values:
            return 1
        n = len(values)
        for i in range(n_bins, 0, -1):
            threshold_idx = int(n * (n_bins - i + 1) / n_bins) - 1
            threshold_idx = max(0, min(threshold_idx, n - 1))
            if reverse:
                if val <= values[threshold_idx]:
                    return i
            else:
                if val >= values[threshold_idx]:
                    return i
        return 1

    for u in users:
        u["r_score"] = _quantile_score(recency_values, u["recency_days"], reverse=True)
        u["f_score"] = _quantile_score(frequency_values, u["frequency"], reverse=False)
        u["m_score"] = _quantile_score(monetary_values, u["monetary"], reverse=False)
        u["rfm_score"] = f"{u['r_score']}{u['f_score']}{u['m_score']}"
        u["segment"] = _rfm_score(u["r_score"], u["f_score"], u["m_score"])

    segment_counts = {}
    segment_monetary = {}
    for u in users:
        seg = u["segment"]
        segment_counts[seg] = segment_counts.get(seg, 0) + 1
        segment_monetary[seg] = segment_monetary.get(seg, 0) + u["monetary"]

    total_users = len(users)
    segments = []
    for seg in sorted(segment_counts.keys(), key=lambda s: _rfm_segment_priority(s)):
        count = segment_counts[seg]
        segments.append({
            "segment": seg,
            "count": count,
            "percentage": round(count / total_users * 100, 2),
            "avg_monetary": round(segment_monetary[seg] / count, 2),
            "total_monetary": round(segment_monetary[seg], 2),
        })

    avg_recency = round(sum(u["recency_days"] for u in users) / total_users, 1)
    avg_frequency = round(sum(u["frequency"] for u in users) / total_users, 2)
    avg_monetary = round(sum(u["monetary"] for u in users) / total_users, 2)

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
        "top_users": sorted(users, key=lambda u: u["r_score"] * 100 + u["f_score"] * 10 + u["m_score"], reverse=True)[:20],
    }


@cached(ttl=300)
async def get_rfm_segment_detail(
    db: AsyncSession,
    segment: str,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    rfm_data = await compute_rfm(db)
    if "error" in rfm_data:
        return rfm_data

    all_users = rfm_data.get("top_users", [])
    for u in rfm_data.get("segments", []):
        if u["segment"] == segment:
            pass

    full_rfm = await _compute_rfm_full(db, rfm_data)
    filtered = [u for u in full_rfm if u["segment"] == segment]

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


async def _compute_rfm_full(db: AsyncSession, rfm_data: dict) -> list:
    ref_date_str = rfm_data.get("reference_date")
    if not ref_date_str:
        return []

    ref_date = datetime.strptime(ref_date_str, "%Y-%m-%d").date()
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
    if not rows:
        return []

    users = [{"user_name": r.user_name, "recency_days": r.recency_days, "frequency": r.frequency, "monetary": round(float(r.monetary), 2)} for r in rows]

    recency_values = sorted([u["recency_days"] for u in users])
    frequency_values = sorted([u["frequency"] for u in users])
    monetary_values = sorted([u["monetary"] for u in users])

    def _quantile_score(values: list, val: float, reverse: bool = False) -> int:
        n = len(values)
        n_bins = 5
        for i in range(n_bins, 0, -1):
            threshold_idx = int(n * (n_bins - i + 1) / n_bins) - 1
            threshold_idx = max(0, min(threshold_idx, n - 1))
            if reverse:
                if val <= values[threshold_idx]:
                    return i
            else:
                if val >= values[threshold_idx]:
                    return i
        return 1

    for u in users:
        u["r_score"] = _quantile_score(recency_values, u["recency_days"], reverse=True)
        u["f_score"] = _quantile_score(frequency_values, u["frequency"], reverse=False)
        u["m_score"] = _quantile_score(monetary_values, u["monetary"], reverse=False)
        u["rfm_score"] = f"{u['r_score']}{u['f_score']}{u['m_score']}"
        u["segment"] = _rfm_score(u["r_score"], u["f_score"], u["m_score"])

    return users


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
