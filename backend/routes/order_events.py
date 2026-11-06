"""订单事件摄入 API。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import func, select

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database import get_db, get_event_db
from backend.models.database_models import DataImportRun, OrderEvent
from backend.models.schemas import OrderEventBatchRequest, OrderEventBatchResponse, OrderEventInput
from backend.utils.auth import SCOPE_DATA_READ, SCOPE_EVENTS_WRITE, Principal, require_scope
from backend.services.order_event_service import apply_order_event
from backend.utils.cache import operational_snapshot
from backend.utils.event_metrics import record_db_timeout, record_event

router = APIRouter(prefix="/api/order-events", tags=["订单事件"])
settings = get_settings()
logger = logging.getLogger(__name__)


def _lag_seconds(occurred_at: datetime) -> float:
    """事件滞后：naive 输入按落库约定视为 UTC，不能当成 0 延迟——那会永久隐藏积压。"""
    occurred = occurred_at.replace(tzinfo=UTC) if occurred_at.tzinfo is None else occurred_at
    return (datetime.now(UTC) - occurred).total_seconds()


@router.post("", response_model=OrderEventBatchResponse, status_code=status.HTTP_200_OK, summary="摄入订单事件")
async def ingest_order_events(
    request: OrderEventInput | OrderEventBatchRequest,
    db: AsyncSession = Depends(get_event_db),
    _principal: Principal = Depends(require_scope(SCOPE_EVENTS_WRITE)),
):
    """接收单条或小批量订单事件。

    单条和批量共享逐条原子事务：一个事件被拒绝不影响同批其他事件，也不会留下
    订单已变但账本未记录的半状态。
    """
    events = request.events if isinstance(request, OrderEventBatchRequest) else [request]
    if len(events) > settings.order_event_batch_max_size:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "EVENT_BATCH_TOO_LARGE", "message": "事件批次超过上限"},
        )

    results = []
    for event in events:
        started = time.perf_counter()
        result = None
        try:
            async with asyncio.timeout(settings.order_event_timeout_seconds):
                result = await apply_order_event(db, event)
        except TimeoutError as exc:
            record_db_timeout()
            raise HTTPException(
                status_code=503,
                detail={"error_code": "EVENT_WRITE_TIMEOUT", "message": "订单事件处理超时，请稍后重试"},
            ) from exc
        except Exception as exc:
            # 服务端保留可诊断日志；API 响应不回显驱动异常、SQL 或连接串。
            logger.exception("订单事件处理失败 event_type=%s order_id=%s", event.event_type, event.order_id)
            raise HTTPException(
                status_code=503,
                detail={"error_code": "EVENT_WRITE_UNAVAILABLE", "message": "订单事件暂时无法处理"},
            ) from exc
        finally:
            lag = _lag_seconds(event.occurred_at)
            # result 尚未可用的异常路径也会有可观测的 received/error 记录。
            outcome = result.status if result is not None else "error"
            record_event(event.event_type, outcome, time.perf_counter() - started, lag)

        results.append(result)

    return OrderEventBatchResponse(
        results=results,
        processed=sum(item.status == "processed" for item in results),
        duplicates=sum(item.status == "duplicate" for item in results),
        rejected=sum(item.status == "rejected" for item in results),
    )


@router.get("/freshness", summary="订单事件与缓存新鲜度")
async def get_event_freshness(
    db: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(require_scope(SCOPE_DATA_READ)),
):
    """返回写入水位、业务事件水位与本进程缓存生成时间；没有事件时不伪造时间戳。

    freshness_seconds 必须基于最近写入时间（processed_at）而不是业务事件时间
    （occurred_at）：导入历史订单时 occurred_at 可能很旧，但数据刚刚更新，
    按业务时间判断会把「刚写完」误报为「数据陈旧」。业务事件时间单独以
    last_event_occurred_at 返回，供需要区分口径的调用方使用。
    """
    row = (await db.execute(
        select(
            func.max(OrderEvent.processed_at).label("last_write_at"),
            func.max(OrderEvent.occurred_at).label("last_event_occurred_at"),
        ).where(OrderEvent.processing_status == "PROCESSED")
    )).one()
    last_import_at = await db.scalar(
        select(func.max(DataImportRun.finished_at))
        .where(DataImportRun.status == "completed")
    )
    cache = operational_snapshot()
    generated_values = list(cache["tag_generated_at"].values())
    generated = max(generated_values) if generated_values else None
    now = datetime.now(UTC)

    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    latest_write = _as_utc(row.last_write_at)
    latest_business = _as_utc(row.last_event_occurred_at)
    latest_import = _as_utc(last_import_at)
    return {
        "data_updated_at": latest_write.isoformat() if latest_write else None,
        "last_event_occurred_at": latest_business.isoformat() if latest_business else None,
        "last_import_at": latest_import.isoformat() if latest_import else None,
        "cache_generated_at": datetime.fromtimestamp(generated, tz=UTC).isoformat() if generated else None,
        # 只按写入时间衡量新鲜度；历史订单导入不会因为业务时间旧而报陈旧。
        "freshness_seconds": (
            round(max((now - latest_write).total_seconds(), 0), 3) if latest_write else None
        ),
        "cache": cache,
    }
