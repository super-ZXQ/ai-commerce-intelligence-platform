import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.services.rfm_service import compute_rfm, get_rfm_segment_detail, get_rfm_overview

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rfm", tags=["RFM用户画像"])


@router.get("/overview", summary="RFM 用户画像总览")
async def rfm_overview(
    db: AsyncSession = Depends(get_db),
):
    result = await get_rfm_overview(db)
    if "error" in result:
        return {"error_code": "RFM_ERROR", "message": result["error"]}
    return result


@router.get("/segments", summary="RFM 用户分群")
async def rfm_segments(
    reference_date: Optional[str] = Query(None, description="参考日期(YYYY-MM-DD)，默认为最新订单日期"),
    n_bins: int = Query(5, ge=3, le=10, description="分位数分组数"),
    db: AsyncSession = Depends(get_db),
):
    result = await compute_rfm(db, reference_date=reference_date, n_bins=n_bins)
    if "error" in result:
        return {"error_code": "RFM_ERROR", "message": result["error"]}
    return {
        "reference_date": result["reference_date"],
        "total_users": result["total_users"],
        "n_bins": result["n_bins"],
        "averages": result["averages"],
        "segments": result["segments"],
    }


@router.get("/segments/{segment}", summary="RFM 分群用户详情")
async def rfm_segment_users(
    segment: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    valid_segments = [
        "重要价值客户", "重要发展客户", "重要保持客户", "重要挽留客户",
        "一般价值客户", "一般发展客户", "一般保持客户", "一般挽留客户",
    ]
    if segment not in valid_segments:
        return {
            "error_code": "INVALID_SEGMENT",
            "message": f"无效分群名称: {segment}",
            "valid_segments": valid_segments,
        }

    result = await get_rfm_segment_detail(db, segment=segment, page=page, page_size=page_size)
    if "error" in result:
        return {"error_code": "RFM_ERROR", "message": result["error"]}
    return result


@router.get("/top-users", summary="RFM TOP 用户")
async def rfm_top_users(
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    db: AsyncSession = Depends(get_db),
):
    result = await compute_rfm(db)
    if "error" in result:
        return {"error_code": "RFM_ERROR", "message": result["error"]}
    return {
        "reference_date": result["reference_date"],
        "total_users": result["total_users"],
        "top_users": result["top_users"][:limit],
    }
