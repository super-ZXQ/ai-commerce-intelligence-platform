import io
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import pandas as pd

from backend.database import get_db
from backend.models.database_models import Order
from backend.services import analytics_service
from backend.routes.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/export", tags=["数据导出"])

_EXPORT_CHUNK_SIZE = 5000


@router.get("/orders", summary="导出订单数据")
async def export_orders(
    export_format: str = Query("csv", description="导出格式: csv/excel"),
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    platform_type: Optional[str] = Query(None, description="平台类型"),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    导出订单数据，支持CSV和Excel格式。

    - **export_format**: 导出格式 (csv/excel)
    - 支持时间范围和平台类型筛选
    - 大数据量导出建议使用CSV格式
    - 采用分批查询避免内存溢出
    """
    conditions = []
    if start_date:
        conditions.append(Order.order_date >= start_date)
    if end_date:
        conditions.append(Order.order_date <= end_date)
    if platform_type:
        conditions.append(Order.platform_type == platform_type)

    count_stmt = select(func.count(Order.id)).where(*conditions)
    total = (await db.execute(count_stmt)).scalar() or 0
    if total == 0:
        raise HTTPException(status_code=404, detail="没有符合条件的订单数据")

    all_data: list[dict] = []
    offset = 0
    while offset < total:
        stmt = (
            select(Order)
            .where(*conditions)
            .order_by(Order.order_time.desc())
            .offset(offset)
            .limit(_EXPORT_CHUNK_SIZE)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        if not rows:
            break
        for o in rows:
            all_data.append({
                "订单编号": o.id,
                "订单号": o.order_no,
                "用户名": o.user_name,
                "商品编号": o.product_id,
                "订单金额": o.order_amount,
                "付款金额": o.payment_amount,
                "平台类型": o.platform_type,
                "下单时间": str(o.order_time),
                "付款时间": str(o.payment_time) if o.payment_time else "",
                "是否退款": o.is_refunded,
                "优惠金额": o.discount_amount,
            })
        offset += len(rows)
        logger.info(f"导出进度: {min(offset, total)}/{total}")

    df = pd.DataFrame(all_data)

    if export_format == "excel":
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="订单数据")
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=orders_export.xlsx"},
        )

    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False, encoding="utf-8-sig")
    csv_buffer.seek(0)
    return StreamingResponse(
        io.BytesIO(csv_buffer.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders_export.csv"},
    )


@router.get("/analytics", summary="导出分析报告")
async def export_analytics(
    export_format: str = Query("csv", description="导出格式: csv/excel"),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    导出分析报告，包含销售总览、趋势、热销商品、用户行为等数据。

    - **export_format**: 导出格式 (csv/excel)
    """
    overview = await analytics_service.get_sales_overview(db)
    trend = await analytics_service.get_sales_trend(db, granularity="day")
    top_products = await analytics_service.get_top_products(db, limit=20)
    user_behavior = await analytics_service.get_user_behavior(db)
    category = await analytics_service.get_category_analysis(db)

    overview_df = pd.DataFrame([overview.model_dump()])
    trend_df = pd.DataFrame([t.model_dump() for t in trend.data])
    products_df = pd.DataFrame([p.model_dump() for p in top_products])
    behavior_df = pd.DataFrame([user_behavior.model_dump()])
    category_df = pd.DataFrame([c.model_dump() for c in category.categories])

    if export_format == "excel":
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            overview_df.to_excel(writer, index=False, sheet_name="销售总览")
            trend_df.to_excel(writer, index=False, sheet_name="销售趋势")
            products_df.to_excel(writer, index=False, sheet_name="热销商品")
            behavior_df.to_excel(writer, index=False, sheet_name="用户行为")
            category_df.to_excel(writer, index=False, sheet_name="平台分析")
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=analytics_report.xlsx"},
        )

    combined = (
        f"=== 销售总览 ===\n{overview_df.to_csv(index=False)}\n"
        f"=== 销售趋势 ===\n{trend_df.to_csv(index=False)}\n"
        f"=== 热销商品 ===\n{products_df.to_csv(index=False)}\n"
        f"=== 用户行为 ===\n{behavior_df.to_csv(index=False)}\n"
        f"=== 平台分析 ===\n{category_df.to_csv(index=False)}\n"
    )
    return StreamingResponse(
        io.BytesIO(combined.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=analytics_report.csv"},
    )
