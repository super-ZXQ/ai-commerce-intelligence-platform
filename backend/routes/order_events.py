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
from backend.database import get_event_db
from backend.models.database_models import OrderEvent
from backend.models.schemas import OrderEventBatchRequest, OrderEventBatchResponse, OrderEventInput
from backend.routes.auth import get_current_user
from backend.services.order_event_service import apply_order_event
from backend.utils.cache import operational_snapshot
from backend.utils.event_metrics import record_db_timeout, record_event

router = APIRouter(prefix="/api/order-events", tags=["订单事件"])
settings = get_settings()
logger = logging.getLogger(__name__)


@router.post("", response_model=OrderEventBatchResponse, status_code=status.HTTP_200_OK, summary="摄入订单事件")
async def ingest_order_events(
    request: OrderEventInput | OrderEventBatchRequest,
    db: AsyncSession = Depends(get_event_db),
    _user: dict = Depends(get_current_user),
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
        except TimeoutError:
            record_db_timeout()
            raise HTTPException(
                status_code=503,
                detail={"error_code": "EVENT_WRITE_TIMEOUT", "message": "订单事件处理超时，请稍后重试"},
            )
        except Exception as exc:
            # 服务端保留可诊断日志；API 响应不回显驱动异常、SQL 或连接串。
            logger.exception("订单事件处理失败 event_type=%s order_id=%s", event.event_type, event.order_id)
            raise HTTPException(
                status_code=503,
                detail={"error_code": "EVENT_WRITE_UNAVAILABLE", "message": "订单事件暂时无法处理"},
            ) from exc
        finally:
            lag = (datetime.now(UTC) - event.occurred_at.astimezone(UTC)).total_seconds() if event.occurred_at.tzinfo else 0
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
    db: AsyncSession = Depends(get_event_db),
    _user: dict = Depends(get_current_user),
):
    """返回真实事件水位与本进程缓存生成时间；没有事件时不伪造时间戳。"""
    row = (await db.execute(
        select(
            func.max(OrderEvent.processed_at).label("data_updated_at"),
            func.max(OrderEvent.occurred_at).label("last_event_occurred_at"),
        ).where(OrderEvent.processing_status == "PROCESSED")
    )).one()
    cache = operational_snapshot()
    generated_values = list(cache["tag_generated_at"].values())
    generated = max(generated_values) if generated_values else None
    now = datetime.now(UTC)
    latest = row.last_event_occurred_at
    if latest is not None and latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    return {
        "data_updated_at": row.data_updated_at.replace(tzinfo=UTC).isoformat() if row.data_updated_at else None,
        "last_event_occurred_at": latest.isoformat() if latest else None,
        "cache_generated_at": datetime.fromtimestamp(generated, tz=UTC).isoformat() if generated else None,
        "freshness_seconds": round(max((now - latest).total_seconds(), 0), 3) if latest else None,
        "cache": cache,
    }
