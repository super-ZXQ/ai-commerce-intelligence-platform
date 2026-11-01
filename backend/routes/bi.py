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
    data_version: int | None = Query(
        None,
        ge=0,
        description="首屏返回的数据版本；后续页必须携带，版本变化时返回 409",
    ),
    db: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(require_scope(SCOPE_DATA_READ)),
):
    """与销售总览使用同一非取消口径；整次分页读取绑定同一数据版本。"""
    if page > 1 and data_version is None:
        # 没有首屏版本就无法保证跨页处于同一快照，宁可拒绝也不返回可能漏行的页。
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "BI_DATA_VERSION_REQUIRED",
                "message": "翻页必须携带首屏返回的 data_version",
            },
        )
    try:
        result = await order_service.get_bi_orders(
            db,
            page,
            page_size,
            expected_version=data_version,
        )
    except order_service.BIDataVersionChanged as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "BI_DATA_VERSION_CHANGED",
                "message": "BI 数据在分页读取期间发生变化，请重新加载",
                "data_version": exc.actual,
            },
        )
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
