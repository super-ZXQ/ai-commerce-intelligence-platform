import logging
from typing import Optional

from sqlalchemy import select, func, case, text
from sqlalchemy.ext.asyncio import AsyncSession
from backend.utils.cache import cached

from backend.models.database_models import Order
from backend.models.schemas import (
    SalesOverviewResponse,
    SalesTrendResponse,
    SalesTrendItem,
    TopProductResponse,
    UserBehaviorResponse,
    CategoryAnalysisResponse,
    CategoryAnalysisItem,
)

logger = logging.getLogger(__name__)


@cached(ttl=120)
async def get_sales_overview(db: AsyncSession) -> SalesOverviewResponse:
    """获取销售总览：总销售额、订单数、客单价、用户数、退款率"""
    stmt = select(
        func.coalesce(func.sum(Order.payment_amount), 0).label("total_sales"),
        func.count(Order.id).label("total_orders"),
        func.count(func.distinct(Order.user_name)).label("total_users"),
        func.sum(
            case((Order.is_refunded == "是", Order.payment_amount), else_=0)
        ).label("total_refund_amount"),
        func.sum(case((Order.is_refunded == "是", 1), else_=0)).label(
            "refund_count"
        ),
    )
    row = (await db.execute(stmt)).one()

    total_sales = round(row.total_sales, 2)
    total_orders = row.total_orders
    avg_order_value = round(total_sales / total_orders, 2) if total_orders > 0 else 0
    refund_rate = round(row.refund_count / total_orders * 100, 2) if total_orders > 0 else 0

    return SalesOverviewResponse(
        total_sales=total_sales,
        total_orders=total_orders,
        avg_order_value=avg_order_value,
        total_users=row.total_users,
        refund_rate=refund_rate,
        total_refund_amount=round(row.total_refund_amount or 0, 2),
    )


@cached(ttl=120)
async def get_sales_trend(
    db: AsyncSession,
    granularity: str = "day",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> SalesTrendResponse:
    """获取销售趋势（按日/周/月聚合）"""
    conditions = []
    if start_date:
        conditions.append(Order.order_date >= start_date)
    if end_date:
        conditions.append(Order.order_date <= end_date)

    if granularity == "week":
        period_expr = func.date_format(Order.order_time, "%Y-W%u").label("period")
    elif granularity == "month":
        period_expr = func.date_format(Order.order_time, "%Y-%m").label("period")
    else:
        period_expr = func.date_format(Order.order_time, "%Y-%m-%d").label("period")

    stmt = (
        select(
            period_expr,
            func.coalesce(func.sum(Order.payment_amount), 0).label("sales"),
            func.count(Order.id).label("order_count"),
        )
        .where(*conditions)
        .group_by("period")
        .order_by("period")
    )
    result = await db.execute(stmt)
    rows = result.all()

    return SalesTrendResponse(
        granularity=granularity,
        data=[
            SalesTrendItem(
                period=row.period,
                sales=round(row.sales, 2),
                order_count=row.order_count,
            )
            for row in rows
        ],
    )


@cached(ttl=120)
async def get_top_products(
    db: AsyncSession,
    limit: int = 10,
) -> list[TopProductResponse]:
    """获取热销商品排名"""
    stmt = (
        select(
            Order.product_id,
            func.count(Order.id).label("order_count"),
            func.sum(Order.payment_amount).label("total_sales"),
        )
        .group_by(Order.product_id)
        .order_by(func.sum(Order.payment_amount).desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.all()

    return [
        TopProductResponse(
            rank=idx + 1,
            product_id=row.product_id,
            order_count=row.order_count,
            total_sales=round(row.total_sales, 2),
        )
        for idx, row in enumerate(rows)
    ]


@cached(ttl=180)
async def get_user_behavior(db: AsyncSession) -> UserBehaviorResponse:
    """用户行为分析：复购率、活跃度等（优化为2次查询）"""
    stats_stmt = select(
        func.count(func.distinct(Order.user_name)).label("total_users"),
        func.count(Order.id).label("total_orders"),
        func.coalesce(func.sum(Order.payment_amount), 0).label("total_sales"),
        func.max(Order.order_date).label("max_date"),
    )
    stats = (await db.execute(stats_stmt)).one()

    total_users = stats.total_users or 0
    total_orders = stats.total_orders or 0
    total_sales = stats.total_sales or 0

    repeat_stmt = (
        select(func.count())
        .select_from(
            select(func.count(Order.id).label("cnt"))
            .group_by(Order.user_name)
            .having(func.count(Order.id) >= 2)
            .subquery()
        )
    )
    repeat_users = (await db.execute(repeat_stmt)).scalar() or 0

    repeat_rate = round(repeat_users / total_users * 100, 2) if total_users > 0 else 0
    avg_freq = round(total_orders / total_users, 2) if total_users > 0 else 0
    avg_value = round(total_sales / total_orders, 2) if total_orders > 0 else 0

    active_7d = 0
    active_30d = 0
    if stats.max_date:
        active_7d_stmt = select(
            func.count(func.distinct(Order.user_name))
        ).where(
            Order.order_date >= func.date_sub(stats.max_date, text("INTERVAL 7 DAY"))
        )
        active_7d = (await db.execute(active_7d_stmt)).scalar() or 0

        active_30d_stmt = select(
            func.count(func.distinct(Order.user_name))
        ).where(
            Order.order_date >= func.date_sub(stats.max_date, text("INTERVAL 30 DAY"))
        )
        active_30d = (await db.execute(active_30d_stmt)).scalar() or 0

    return UserBehaviorResponse(
        total_users=total_users,
        repeat_purchase_rate=repeat_rate,
        avg_order_frequency=avg_freq,
        avg_customer_value=avg_value,
        active_users_7d=active_7d,
        active_users_30d=active_30d,
    )


@cached(ttl=180)
async def get_category_analysis(db: AsyncSession) -> CategoryAnalysisResponse:
    """品类/平台分析"""
    total_sales_result = await db.execute(
        select(func.coalesce(func.sum(Order.payment_amount), 0))
    )
    total_sales = total_sales_result.scalar() or 1

    # 显式 .label() 命名，避免下游 row[i] 位置索引脆性
    stmt = select(
        Order.platform_type.label("platform_type"),
        func.count(Order.id).label("order_count"),
        func.sum(Order.payment_amount).label("total_sales"),
        (func.sum(Order.payment_amount) / func.count(Order.id)).label("avg_order_value"),
        (
            func.sum(case((Order.is_refunded == "是", 1), else_=0))
            / func.count(Order.id)
            * 100
        ).label("refund_rate"),
    ).group_by(Order.platform_type)

    result = await db.execute(stmt)
    rows = result.all()

    return CategoryAnalysisResponse(
        categories=[
            CategoryAnalysisItem(
                platform_type=row.platform_type,
                order_count=row.order_count,
                total_sales=round(float(row.total_sales), 2),
                sales_ratio=round(float(row.total_sales) / total_sales * 100, 2),
                avg_order_value=round(float(row.avg_order_value), 2),
                refund_rate=round(float(row.refund_rate), 2),
            )
            for row in rows
        ]
    )
