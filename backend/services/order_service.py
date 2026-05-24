import logging
import math
from typing import Optional

from sqlalchemy import select, func, desc, asc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database_models import Order
from backend.models.schemas import (
    OrderResponse,
    OrderFilterParams,
    ProductSalesResponse,
    UserSpendingResponse,
    PaginatedResponse,
)

logger = logging.getLogger(__name__)

SORT_COLUMN_MAP = {
    "order_time": Order.order_time,
    "payment_amount": Order.payment_amount,
    "order_amount": Order.order_amount,
    "order_no": Order.order_no,
}


async def get_orders(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    sort_by: str = "order_time",
    sort_order: str = "desc",
) -> PaginatedResponse:
    """获取订单列表（分页+排序）"""
    count_stmt = select(func.count(Order.id))
    total = (await db.execute(count_stmt)).scalar() or 0

    sort_col = SORT_COLUMN_MAP.get(sort_by, Order.order_time)
    order_func = desc if sort_order == "desc" else asc

    stmt = (
        select(Order)
        .order_by(order_func(sort_col))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    orders = result.scalars().all()

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if page_size > 0 else 0,
        items=[OrderResponse.model_validate(o) for o in orders],
    )


async def get_order_by_id(db: AsyncSession, order_id: int) -> Optional[OrderResponse]:
    """根据ID获取单个订单详情"""
    stmt = select(Order).where(Order.id == order_id)
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()
    if order is None:
        return None
    return OrderResponse.model_validate(order)


async def filter_orders(
    db: AsyncSession,
    filters: OrderFilterParams,
    page: int = 1,
    page_size: int = 20,
) -> PaginatedResponse:
    """条件筛选订单"""
    conditions = []

    if filters.start_date:
        conditions.append(Order.order_date >= filters.start_date)
    if filters.end_date:
        conditions.append(Order.order_date <= filters.end_date)
    if filters.platform_type:
        conditions.append(Order.platform_type == filters.platform_type)
    if filters.user_name:
        escaped = filters.user_name.replace("%", r"\%").replace("_", r"\_")
        conditions.append(Order.user_name.like(f"%{escaped}%", escape="\\"))
    if filters.product_id:
        conditions.append(Order.product_id == filters.product_id)
    if filters.is_refunded is not None:
        conditions.append(Order.is_refunded == filters.is_refunded)
    if filters.min_amount is not None:
        conditions.append(Order.payment_amount >= filters.min_amount)
    if filters.max_amount is not None:
        conditions.append(Order.payment_amount <= filters.max_amount)

    count_stmt = select(func.count(Order.id)).where(*conditions)
    total = (await db.execute(count_stmt)).scalar() or 0

    sort_col = SORT_COLUMN_MAP.get(filters.sort_by, Order.order_time)
    order_func = desc if filters.sort_order == "desc" else asc

    stmt = (
        select(Order)
        .where(*conditions)
        .order_by(order_func(sort_col))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    orders = result.scalars().all()

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if page_size > 0 else 0,
        items=[OrderResponse.model_validate(o) for o in orders],
    )


async def get_product_sales(
    db: AsyncSession,
    limit: int = 20,
) -> list[ProductSalesResponse]:
    """获取商品销售排名"""
    stmt = (
        select(
            Order.product_id,
            func.count(Order.id).label("order_count"),
            func.sum(Order.payment_amount).label("total_sales"),
        )
        .group_by(Order.product_id)
        .order_by(desc(func.sum(Order.payment_amount)))
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.all()

    return [
        ProductSalesResponse(
            product_id=row.product_id,
            order_count=row.order_count,
            total_sales=round(row.total_sales, 2),
        )
        for row in rows
    ]


async def get_user_spending(
    db: AsyncSession,
    limit: int = 20,
) -> list[UserSpendingResponse]:
    """获取用户消费排名"""
    stmt = (
        select(
            Order.user_name,
            func.count(Order.id).label("order_count"),
            func.sum(Order.payment_amount).label("total_spent"),
        )
        .group_by(Order.user_name)
        .order_by(desc(func.sum(Order.payment_amount)))
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.all()

    return [
        UserSpendingResponse(
            user_name=row.user_name,
            order_count=row.order_count,
            total_spent=round(row.total_spent, 2),
            avg_order_value=round(row.total_spent / row.order_count, 2),
        )
        for row in rows
    ]
