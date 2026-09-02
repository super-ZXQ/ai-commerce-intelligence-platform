import csv
import io
import logging
from datetime import date

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.database_models import Order
from backend.routes.auth import get_current_user
from backend.services import analytics_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/export", tags=["数据导出"])

_EXPORT_CHUNK_SIZE = 5000
_ORDER_EXPORT_HEADERS = [
    "订单编号", "订单号", "用户名", "商品编号", "订单金额", "付款金额",
    "平台类型", "下单时间", "付款时间", "是否退款", "优惠金额",
]


def _sanitize_cell(value):
    """防 CSV/Excel 公式注入：以 = + - @ 或控制符开头的单元格加 ' 前缀。

    数据库中的 user_name / platform_type 等字符串字段可能被注入恶意公式，
    用户打开导出文件时会被 Excel/WPS 当作公式执行（如 DDE 外链）。
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{value}"
    return value


def _order_export_row(order: Order) -> list:
    """将 ORM 订单转换为导出列，CSV 与 Excel 共用同一字段顺序。"""
    return [
        _sanitize_cell(order.id),
        _sanitize_cell(order.order_no),
        _sanitize_cell(order.user_name),
        _sanitize_cell(order.product_id),
        _sanitize_cell(order.order_amount),
        _sanitize_cell(order.payment_amount),
        _sanitize_cell(order.platform_type),
        _sanitize_cell(str(order.order_time)),
        _sanitize_cell(str(order.payment_time) if order.payment_time else ""),
        _sanitize_cell(order.is_refunded),
        _sanitize_cell(order.discount_amount),
    ]


async def _iter_order_chunks(
    db: AsyncSession,
    conditions: list,
    total: int,
):
    """按固定大小读取订单，避免一次性加载整个导出结果。"""
    offset = 0
    while offset < total:
        stmt = (
            select(Order)
            .where(*conditions)
            # order_time 并列值极多，MySQL 对并列行排序不稳定，
            # 必须补 id 作次级排序键，否则 OFFSET 分页会重复/丢行
            .order_by(Order.order_time.desc(), Order.id.desc())
            .offset(offset)
            .limit(_EXPORT_CHUNK_SIZE)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        if not rows:
            break
        yield rows
        offset += len(rows)
        logger.info("导出进度: %s/%s", min(offset, total), total)


async def _stream_orders_csv(
    db: AsyncSession,
    conditions: list,
    total: int,
):
    """逐块生成 UTF-8 BOM CSV，首字节可在首批数据库结果返回后发送。"""
    yield "\ufeff".encode("utf-8")

    header_buffer = io.StringIO()
    csv.writer(header_buffer, lineterminator="\n").writerow(_ORDER_EXPORT_HEADERS)
    yield header_buffer.getvalue().encode("utf-8")

    async for rows in _iter_order_chunks(db, conditions, total):
        chunk_buffer = io.StringIO()
        writer = csv.writer(chunk_buffer, lineterminator="\n")
        writer.writerows(_order_export_row(order) for order in rows)
        yield chunk_buffer.getvalue().encode("utf-8")


@router.get("/orders", summary="导出订单数据")
async def export_orders(
    export_format: str = Query("csv", pattern="^(csv|excel)$", description="导出格式: csv/excel"),
    start_date: date | None = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: date | None = Query(None, description="结束日期 YYYY-MM-DD"),
    platform_type: str | None = Query(None, description="平台类型"),
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
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")

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
        return {
            "total": 0,
            "items": [],
            "message": "没有符合条件的订单数据",
        }

    if export_format == "csv":
        return StreamingResponse(
            _stream_orders_csv(db, conditions, total),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=orders_export.csv"},
        )

    # Excel 格式需要先构建完整工作簿；CSV 使用上方流式路径避免大对象驻留内存。
    all_data: list[list] = []
    async for rows in _iter_order_chunks(db, conditions, total):
        all_data.extend(_order_export_row(order) for order in rows)
    df = pd.DataFrame(all_data, columns=_ORDER_EXPORT_HEADERS)

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

@router.get("/analytics", summary="导出分析报告")
async def export_analytics(
    export_format: str = Query("csv", pattern="^(csv|excel)$", description="导出格式: csv/excel"),
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
