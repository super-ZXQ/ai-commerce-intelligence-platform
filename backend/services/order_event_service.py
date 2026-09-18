"""订单事件的幂等、乱序保护和读模型更新。

这里不是异步消息队列的替代品：本项目以 MySQL 事务作为最小可靠闭环。事件账本
与 orders 读模型在同一事务提交，因此 API 成功时两者同时可见，失败时两者都回滚。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database_models import Order, OrderEvent
from backend.models.schemas import (
    OrderCreatedPayload,
    OrderEventInput,
    OrderEventResult,
    OrderStatePayload,
)
from backend.services.rfm_service import clear_rfm_snapshot_cache
from backend.utils.cache import invalidate_tags

_ACTIVE = "ACTIVE"
_REFUNDED = "REFUNDED"
_CANCELLED = "CANCELLED"
_PROCESSED = "PROCESSED"
_REJECTED = "REJECTED"


def _utc_naive(value: datetime) -> datetime:
    """MySQL DATETIME 不保存时区；统一落 UTC，避免事件源时区影响版本时序审计。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_now_naive() -> datetime:
    """为 MySQL DATETIME 生成不带时区、但语义为 UTC 的当前时间。"""
    return datetime.now(UTC).replace(tzinfo=None)


def _result(event: OrderEventInput, status: str, reason: str | None = None) -> OrderEventResult:
    return OrderEventResult(
        event_id=event.event_id,
        order_id=event.order_id,
        event_type=event.event_type,
        status=status,
        reason=reason,
        data_updated_at=datetime.now(UTC) if status == "processed" else None,
    )


def _reject(ledger: OrderEvent, event: OrderEventInput, reason: str) -> OrderEventResult:
    ledger.processing_status = _REJECTED
    ledger.processed_at = _utc_now_naive()
    ledger.failure_reason = reason
    return _result(event, "rejected", reason)


async def _insert_ledger_or_duplicate(
    db: AsyncSession, event: OrderEventInput
) -> tuple[OrderEvent | None, OrderEventResult | None]:
    """插入唯一 event_id；并发重复以数据库约束为准，而非进程内锁。"""
    existing = await db.scalar(select(OrderEvent).where(OrderEvent.event_id == str(event.event_id)))
    if existing is not None:
        return None, _result(event, "duplicate", "event_id 已处理")

    ledger = OrderEvent(
        event_id=str(event.event_id),
        order_no=event.order_id,
        event_type=event.event_type,
        occurred_at=_utc_naive(event.occurred_at),
        sequence=event.version,
        source=event.source,
        payload=event.payload,
        processing_status="RECEIVED",
    )
    try:
        # 保存点让唯一键竞争只回滚本次 INSERT；外层事务仍可查询赢家并返回幂等结果。
        async with db.begin_nested():
            db.add(ledger)
            await db.flush()
    except IntegrityError:
        return None, _result(event, "duplicate", "event_id 已处理")
    return ledger, None


def _new_order(
    event: OrderEventInput,
    payload: OrderCreatedPayload,
    order_time: datetime,
    *,
    order_seq_id: int | None = None,
) -> Order:
    """将已校验的创建事件投影为 orders 读模型行。

    order_seq_id：Docker 旧卷/历史 schema 可能没有 AUTO_INCREMENT，因此调用方
    应显式传入主键；为 None 时依赖数据库自增。
    """
    return Order(
        id=order_seq_id,
        order_no=event.order_id,
        user_name=payload.user_name,
        product_id=payload.product_id,
        order_amount=payload.order_amount,
        payment_amount=payload.payment_amount,
        channel_id=payload.channel_id,
        platform_type=payload.platform_type,
        order_time=order_time,
        payment_time=order_time,
        is_refunded="否",
        discount_amount=payload.discount_amount,
        payment_duration_sec=0,
        order_date=order_time.date(),
        order_hour=order_time.hour,
        weekday=order_time.strftime("%A"),
        order_status=_ACTIVE,
        event_version=event.version,
        updated_at=_utc_now_naive(),
    )


async def apply_order_event(db: AsyncSession, event: OrderEventInput) -> OrderEventResult:
    """原子应用一条事件。

    乱序策略：版本号必须严格大于订单已应用版本；低版本或同版本但 event_id 不同的
    事件会以 REJECTED 留在账本。已终态订单不接受另一种终态覆盖。
    """
    async with db.begin():
        result = await _apply_in_transaction(db, event)
    if result.status == "processed":
        # 只在事务提交后失效受影响的视图，避免失败写入误删有效缓存。
        await invalidate_tags("analytics", "rfm")
        clear_rfm_snapshot_cache()
    return result


async def _apply_in_transaction(db: AsyncSession, event: OrderEventInput) -> OrderEventResult:
    """在调用方已开启的事务内写账本与订单读模型。"""
    ledger, duplicate = await _insert_ledger_or_duplicate(db, event)
    if duplicate is not None:
        return duplicate
    assert ledger is not None

    order = await db.scalar(
        select(Order).where(Order.order_no == event.order_id).with_for_update()
    )

    if event.event_type == "ORDER_CREATED":
        if order is not None:
            if event.version <= order.event_version:
                return _reject(ledger, event, "事件版本不高于当前订单版本")
            return _reject(ledger, event, "订单已存在，拒绝重复创建")
        payload = OrderCreatedPayload.model_validate(event.payload)
        order_time = _utc_naive(payload.order_time or event.occurred_at)
        # 显式分配主键：不依赖目标库是否配置了 AUTO_INCREMENT。
        max_seq = await db.scalar(select(func.max(Order.id)))
        db.add(_new_order(event, payload, order_time, order_seq_id=(max_seq or 0) + 1))
        ledger.processing_status = _PROCESSED
        ledger.processed_at = _utc_now_naive()
        return _result(event, "processed")

    if order is None:
        return _reject(ledger, event, "订单不存在，无法应用状态事件")
    if event.version <= order.event_version:
        return _reject(ledger, event, "事件版本不高于当前订单版本")

    # 重新验证，确保退款/取消不会通过任意 JSON 偷改订单金额或身份信息。
    OrderStatePayload.model_validate(event.payload)
    target_status = _REFUNDED if event.event_type == "ORDER_REFUNDED" else _CANCELLED
    if order.order_status != _ACTIVE:
        return _reject(ledger, event, f"订单当前为 {order.order_status}，不能转换为 {target_status}")

    order.order_status = target_status
    order.is_refunded = "是" if target_status == _REFUNDED else order.is_refunded
    order.event_version = event.version
    order.updated_at = _utc_now_naive()
    ledger.processing_status = _PROCESSED
    ledger.processed_at = _utc_now_naive()
    return _result(event, "processed")
