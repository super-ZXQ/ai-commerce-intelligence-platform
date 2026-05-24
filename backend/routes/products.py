import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.schemas import (
    ProductSalesResponse,
    UserSpendingResponse,
)
from backend.services import order_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["商品与用户"])


@router.get("/products", response_model=list[ProductSalesResponse], summary="获取商品销售排名")
async def list_products(
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取商品销售排名列表，按销售额降序排列。

    - **limit**: 返回的商品数量，默认20
    """
    return await order_service.get_product_sales(db, limit)


@router.get("/users", response_model=list[UserSpendingResponse], summary="获取用户消费排名")
async def list_users(
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取用户消费排名列表，按消费总额降序排列。

    - **limit**: 返回的用户数量，默认20
    """
    return await order_service.get_user_spending(db, limit)
