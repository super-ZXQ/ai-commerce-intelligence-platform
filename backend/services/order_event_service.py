"""订单事件的幂等、乱序保护和读模型更新。

这里不是异步消息队列的替代品：本项目以 MySQL 事务作为最小可靠闭环。事件账本
与 orders 读模型在同一事务提交，因此 API 成功时两者同时可见，失败时两者都回滚。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
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

logger = logging.getLogger(__name__)

_ACTIVE = "ACTIVE"
_REFUNDED = "REFUNDED"
_CANCELLED = "CANCELLED"
_PROCESSED = "PROCESSED"
_REJECTED = "REJECTED"


_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _utc_naive(value: datetime) -> datetime:
    """MySQL DATETIME 不保存时区；运维/事件时间统一落 UTC。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _business_naive(value: datetime) -> datetime:
    """业务日期/时间按 Asia/Shanghai 墙钟落库，不做 UTC 换算。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(_BUSINESS_TZ).replace(tzinfo=None)


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


async def _bump_data_version(db: AsyncSession) -> None:
    """在事件事务内递增全局数据版本。

    单行更新会串行化并发事件事务，这是刻意选择：版本号必须与订单/账本
    提交顺序一致，缓存才能按同一快照切换。事件写入是低频管理操作，
    单行锁不会成为查询链路的瓶颈。
    """
    await db.execute(text("UPDATE data_state SET data_version = data_version + 1 WHERE id = 1"))


def _new_order(
    event: OrderEventInput,
    payload: OrderCreatedPayload,
    order_time: datetime,
    *,
    order_seq_id: int | None = None,
) -> Order:
    """将已校验的创建事件投影为 orders 读模型行。

    order_seq_id 为 None 时由数据库分配，避免并发事务读取相同的最大值。
    显式主键参数保留给需要导入已有编号的调用方。
    """
    order_time = _business_naive(order_time)
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


async def _replay_rejected_state_events(db: AsyncSession, order: Order) -> int:
    """创建订单时重放此前因「订单不存在」被拒的退款/取消事件。

    事件账本已经保存了 payload 与 version，不需要新增死信表。只重放最早的一条
    终态事件：订单只能进入 REFUNDED 或 CANCELLED 中的一个终态；其余仍保持
    REJECTED，避免用后续事件覆盖已确定的终态。
    """
    rows = (
        await db.execute(
            select(OrderEvent)
            .where(
                OrderEvent.order_no == order.order_no,
                OrderEvent.processing_status == _REJECTED,
                OrderEvent.event_type.in_(("ORDER_REFUNDED", "ORDER_CANCELLED")),
            )
            .order_by(OrderEvent.sequence.asc(), OrderEvent.id.asc())
        )
    ).scalars().all()

    for ledger in rows:
        if ledger.sequence <= order.event_version:
            continue
        if order.order_status != _ACTIVE:
            break
        OrderStatePayload.model_validate(ledger.payload)
        target_status = _REFUNDED if ledger.event_type == "ORDER_REFUNDED" else _CANCELLED
        order.order_status = target_status
        order.is_refunded = "是" if target_status == _REFUNDED else order.is_refunded
        order.event_version = ledger.sequence
        order.updated_at = _utc_now_naive()
        ledger.processing_status = _PROCESSED
        ledger.processed_at = _utc_now_naive()
        ledger.failure_reason = None
        logger.info(
            "重放订单状态事件: order_id=%s event_type=%s version=%s",
            order.order_no,
            ledger.event_type,
            ledger.sequence,
        )
        return 1
    return 0


async def _apply_in_transaction(db: AsyncSession, event: OrderEventInput) -> OrderEventResult:
    """在调用方已开启的事务内写账本与订单读模型。"""
    ledger, duplicate = await _insert_ledger_or_duplicate(db, event)
    if duplicate is not None:
        return duplicate
    if ledger is None:
        # 插入账本后必然返回记录；显式失败比 assert 更可靠
        # （python -O 会移除 assert，导致后续 None 解引用）。
        raise RuntimeError("订单事件账本写入后未返回记录")

    order_query = select(Order).where(Order.order_no == event.order_id)
    # 创建时无需锁定不存在的订单；MySQL 的间隙锁会让不同订单并发插入互相等待。
    # 已有订单的退款、取消仍需行锁保护状态转换。
    if event.event_type != "ORDER_CREATED":
        order_query = order_query.with_for_update()
    order = await db.scalar(order_query)

    if event.event_type == "ORDER_CREATED":
        if order is not None:
            if event.version <= order.event_version:
                return _reject(ledger, event, "事件版本不高于当前订单版本")
            return _reject(ledger, event, "订单已存在，拒绝重复创建")
        payload = OrderCreatedPayload.model_validate(event.payload)
        # 业务日期字段看的是 Asia/Shanghai 墙钟；事件 occurred_at 是 UTC，
        # 缺失 payload.order_time 时必须先换算到业务时区，否则日期/小时会跨天。
        order_time = _business_naive(payload.order_time or event.occurred_at)
        # 自增由数据库原子分配；历史卷须先执行 Alembic 升级。
        order = _new_order(event, payload, order_time)
        db.add(order)
        # 若退款/取消先于创建到达，它们在账本里是 REJECTED；创建成功后
        # 立刻在同一个事务里重放，避免账本与读模型永久分叉。
        replayed = await _replay_rejected_state_events(db, order)
        await _bump_data_version(db)
        ledger.processing_status = _PROCESSED
        ledger.processed_at = _utc_now_naive()
        if replayed:
            logger.info("创建订单后已重放 %d 条先到的状态事件: %s", replayed, event.order_id)
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
    await _bump_data_version(db)
    ledger.processing_status = _PROCESSED
    ledger.processed_at = _utc_now_naive()
    return _result(event, "processed")
