import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.services import abtest_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/abtest", tags=["A/B实验"])


@router.get("/groups", summary="获取可用分组")
async def get_available_groups(
    dimension: str = Query("platform_type", description="分组维度: platform_type / is_refunded / weekday"),
    db: AsyncSession = Depends(get_db),
):
    return await abtest_service.get_available_groups(db, dimension)


@router.get("/compare", summary="A/B双组对比实验")
async def ab_compare(
    dimension: str = Query("platform_type", description="分组维度"),
    group_a: str = Query(..., description="实验组值"),
    group_b: str = Query(..., description="对照组值"),
    metric: str = Query("payment_amount", description="指标: payment_amount / order_amount / discount_amount"),
    alpha: float = Query(0.05, ge=0.01, le=0.10, description="显著性水平"),
    db: AsyncSession = Depends(get_db),
):
    return await abtest_service.run_ab_test(db, dimension, group_a, group_b, metric, alpha)


@router.get("/anova", summary="多组方差分析(ANOVA)")
async def anova_analysis(
    dimension: str = Query("platform_type", description="分组维度"),
    metric: str = Query("payment_amount", description="指标字段"),
    alpha: float = Query(0.05, ge=0.01, le=0.10, description="显著性水平"),
    db: AsyncSession = Depends(get_db),
):
    return await abtest_service.run_multi_group(db, dimension, metric, alpha)
