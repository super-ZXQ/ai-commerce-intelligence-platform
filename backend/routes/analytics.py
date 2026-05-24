import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.schemas import (
    SalesOverviewResponse,
    SalesTrendResponse,
    TopProductResponse,
    UserBehaviorResponse,
    CategoryAnalysisResponse,
)
from backend.services import analytics_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["数据分析"])


@router.get("/sales-overview", response_model=SalesOverviewResponse, summary="销售总览")
async def sales_overview(db: AsyncSession = Depends(get_db)):
    """
    获取销售总览数据，包含：

    - 总销售额
    - 总订单数
    - 平均客单价
    - 总用户数
    - 退款率
    - 退款总额
    """
    return await analytics_service.get_sales_overview(db)


@router.get("/sales-trend", response_model=SalesTrendResponse, summary="销售趋势")
async def sales_trend(
    granularity: str = Query("day", description="聚合粒度: day/week/month"),
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取销售趋势数据，支持按日/周/月聚合。

    - **granularity**: 聚合粒度 (day=按日, week=按周, month=按月)
    - **start_date**: 可选，开始日期
    - **end_date**: 可选，结束日期
    """
    if granularity not in ("day", "week", "month"):
        granularity = "day"
    return await analytics_service.get_sales_trend(db, granularity, start_date, end_date)


@router.get("/top-products", response_model=list[TopProductResponse], summary="热销商品排名")
async def top_products(
    limit: int = Query(10, ge=1, le=50, description="返回数量"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取热销商品排名，按销售额降序。

    - **limit**: 返回排名数量，默认10
    """
    return await analytics_service.get_top_products(db, limit)


@router.get("/user-behavior", response_model=UserBehaviorResponse, summary="用户行为分析")
async def user_behavior(db: AsyncSession = Depends(get_db)):
    """
    获取用户行为分析数据，包含：

    - 总用户数
    - 复购率
    - 平均消费频次
    - 平均客单价
    - 7日/30日活跃用户数
    """
    return await analytics_service.get_user_behavior(db)


@router.get("/category-analysis", response_model=CategoryAnalysisResponse, summary="平台/品类分析")
async def category_analysis(db: AsyncSession = Depends(get_db)):
    """
    获取各平台/品类分析数据，包含：

    - 各平台订单量
    - 各平台销售额及占比
    - 各平台客单价
    - 各平台退款率
    """
    return await analytics_service.get_category_analysis(db)
