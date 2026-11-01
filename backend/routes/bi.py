"""BI 看板的运行期数据入口。

Streamlit 不再持有数据库凭据或直接查询 orders；页面每次加载都携带当前账号
的 HttpOnly Cookie 调用本路由，由后端重新校验账号状态与 data:read 权限。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database import get_db
from backend.models.schemas import BIOrderPage
from backend.services import order_service
from backend.utils.auth import SCOPE_DATA_READ, Principal, require_scope

router = APIRouter(prefix="/api/bi", tags=["BI 看板"])
settings = get_settings()


@router.get("/orders", response_model=BIOrderPage, summary="BI 订单快照（非取消订单）")
async def bi_orders(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(5000, ge=1, le=20000, description="每页数量"),
    db: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(require_scope(SCOPE_DATA_READ)),
):
    """与销售总览使用同一非取消口径；超过导出上限时拒绝，避免页面只看到部分数据。"""
    result = await order_service.get_bi_orders(db, page, page_size)
    if result.total > settings.export_max_rows:
        raise HTTPException(
            status_code=413,
            detail={
                "error_code": "BI_SNAPSHOT_TOO_LARGE",
                "message": (
                    f"非取消订单 {result.total:,} 行超过页面加载上限 "
                    f"{settings.export_max_rows:,} 行，请联系管理员调整 EXPORT_MAX_ROWS"
                ),
            },
        )
    return result
