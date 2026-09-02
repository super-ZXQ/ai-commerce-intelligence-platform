import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.routes.auth import get_current_user
from backend.services.rfm_service import (
    VALID_SEGMENTS,
    compute_rfm,
    get_rfm_overview,
    get_rfm_segment_detail,
    get_rfm_top_users,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rfm", tags=["RFM用户画像"])


@router.get("/overview", summary="RFM 用户画像总览")
async def rfm_overview(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    result = await get_rfm_overview(db)
    return result


@router.get("/segments", summary="RFM 用户分群")
async def rfm_segments(
    reference_date: date | None = Query(None, description="参考日期(YYYY-MM-DD)，默认为最新订单日期"),
    n_bins: int = Query(5, ge=3, le=10, description="分位数分组数"),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    result = await compute_rfm(
        db,
        reference_date=reference_date.isoformat() if reference_date else None,
        n_bins=n_bins,
    )
    return {
        "reference_date": result["reference_date"],
        "total_users": result["total_users"],
        "n_bins": result["n_bins"],
        "score_threshold": result["score_threshold"],
        "averages": result["averages"],
        "segments": result["segments"],
    }


@router.get("/segments/{segment}", summary="RFM 分群用户详情")
async def rfm_segment_users(
    segment: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    reference_date: date | None = Query(None, description="参考日期(YYYY-MM-DD)，默认为最新订单日期"),
    n_bins: int = Query(5, ge=3, le=10, description="分位数分组数"),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    if segment not in VALID_SEGMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"无效分群名称: {segment}，可选值: {VALID_SEGMENTS}",
        )

    result = await get_rfm_segment_detail(
        db, segment=segment, page=page, page_size=page_size,
        reference_date=reference_date.isoformat() if reference_date else None,
        n_bins=n_bins,
    )
    return result


@router.get("/top-users", summary="RFM TOP 用户")
async def rfm_top_users(
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    reference_date: date | None = Query(None, description="参考日期(YYYY-MM-DD)，默认为最新订单日期"),
    n_bins: int = Query(5, ge=3, le=10, description="分位数分组数"),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    result = await get_rfm_top_users(
        db, limit=limit,
        reference_date=reference_date.isoformat() if reference_date else None,
        n_bins=n_bins,
    )
    return result
