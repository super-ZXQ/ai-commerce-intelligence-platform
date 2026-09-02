import logging
from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.schemas import (
    OrderFilterParams,
    OrderResponse,
    PaginatedResponse,
)
from backend.routes.auth import get_current_user
from backend.services import order_service
from backend.services.order_service import SORT_COLUMN_MAP

logger = logging.getLogger(__name__)

VALID_SORT_FIELDS = list(SORT_COLUMN_MAP.keys())

router = APIRouter(prefix="/api/orders", tags=["订单查询"])


@router.get("", response_model=PaginatedResponse, summary="获取订单列表")
async def list_orders(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    sort_by: str = Query("order_time", description="排序字段"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$", description="排序方向 asc/desc"),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    获取所有订单列表，支持分页和排序。

    - **page**: 页码，从1开始
    - **page_size**: 每页数量，最大100
    - **sort_by**: 排序字段 (order_time, payment_amount, order_amount, order_no)
    - **sort_order**: 排序方向 (asc, desc)
    """
    if sort_by not in SORT_COLUMN_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"无效排序字段 '{sort_by}'，可选值: {VALID_SORT_FIELDS}",
        )
    return await order_service.get_orders(db, page, page_size, sort_by, sort_order)


@router.get("/filter", response_model=PaginatedResponse, summary="条件筛选订单")
async def filter_orders(
    start_date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="开始日期 YYYY-MM-DD"),
    end_date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="结束日期 YYYY-MM-DD"),
    platform_type: str | None = Query(None, description="平台类型"),
    user_name: str | None = Query(None, description="用户名(模糊匹配)"),
    product_id: str | None = Query(None, description="商品编号"),
    is_refunded: str | None = Query(None, description="是否退款 是/否"),
    min_amount: float | None = Query(None, ge=0, description="最小付款金额"),
    max_amount: float | None = Query(None, ge=0, description="最大付款金额"),
    sort_by: str = Query("order_time", description="排序字段"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$", description="排序方向"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    条件筛选订单，支持多维度过滤。

    - 支持时间范围、平台类型、用户名、商品编号、退款状态、金额范围筛选
    - 用户名支持模糊匹配
    """
    parsed_start = date_type.fromisoformat(start_date) if start_date else None
    parsed_end = date_type.fromisoformat(end_date) if end_date else None
    if parsed_start and parsed_end and parsed_start > parsed_end:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise HTTPException(status_code=400, detail="最小金额不能大于最大金额")
    if sort_by not in SORT_COLUMN_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"无效排序字段 '{sort_by}'，可选值: {VALID_SORT_FIELDS}",
        )

    filters = OrderFilterParams(
        start_date=parsed_start,
        end_date=parsed_end,
        platform_type=platform_type,
        user_name=user_name,
        product_id=product_id,
        is_refunded=is_refunded,
        min_amount=min_amount,
        max_amount=max_amount,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return await order_service.filter_orders(db, filters, page, page_size)


@router.get("/{order_id}", response_model=OrderResponse, summary="获取订单详情")
async def get_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    根据订单ID获取单个订单详情。
    """
    result = await order_service.get_order_by_id(db, order_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"订单 {order_id} 不存在")
    return result
