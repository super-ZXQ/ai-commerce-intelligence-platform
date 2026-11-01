"""最小可复现测试：ORDER_CREATED 主键分配与 sales-trend NULL period。"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import asyncio
import os
from uuid import uuid4

import pytest

from backend.models.schemas import OrderCreatedPayload, OrderEventInput
from backend.services.order_event_service import _new_order


def _event() -> OrderEventInput:
    return OrderEventInput(
        event_id="11111111-1111-1111-1111-111111111111",
        order_id="AUDIT-UNIT-1",
        event_type="ORDER_CREATED",
        occurred_at=datetime.now(UTC),
        version=1,
        payload={
            "user_name": "u1",
            "product_id": "P1",
            "order_amount": "100.00",
            "payment_amount": "80.00",
            "platform_type": "APP",
            "channel_id": "c1",
            "discount_amount": "20.00",
        },
        source="unit-test",
    )


def test_new_order_assigns_explicit_seq_id():
    """回归：Docker SQL 历史卷可能无 AUTO_INCREMENT，创建事件必须能带主键。"""
    event = _event()
    payload = OrderCreatedPayload.model_validate(event.payload)
    order = _new_order(event, payload, datetime.now(UTC), order_seq_id=102288)
    assert order.id == 102288
    assert order.order_no == "AUDIT-UNIT-1"
    assert order.payment_amount == Decimal("80.00")
    assert order.order_status == "ACTIVE"
    assert order.event_version == 1


def test_new_order_uses_database_generated_id():
    event = _event()
    order = _new_order(event, OrderCreatedPayload.model_validate(event.payload), datetime.now(UTC))
    assert order.id is None


@pytest.mark.asyncio
async def test_concurrent_order_creation_on_isolated_mysql(monkeypatch):
    """仅在明确指定隔离测试库时并发写入，并清理本测试的行。"""
    if os.environ.get("TEST_ORDER_CONCURRENCY") != "1":
        pytest.skip("仅在隔离 MySQL 测试库运行")
    from sqlalchemy import delete, select, text

    from backend.database import event_engine, event_session_factory
    from backend.models.database_models import Order, OrderEvent
    from backend.services import order_event_service

    async def no_invalidation(*_args):
        return None

    monkeypatch.setattr(order_event_service, "invalidate_tags", no_invalidation)
    monkeypatch.setattr(order_event_service, "clear_rfm_snapshot_cache", lambda: None)
    events = []
    for _ in range(6):
        data = _event().model_dump()
        data.update(event_id=uuid4(), order_id=f"TEST-{uuid4().hex}")
        events.append(OrderEventInput.model_validate(data))

    async def create(event):
        async with event_session_factory() as session:
            return await order_event_service.apply_order_event(session, event)

    order_ids = [event.order_id for event in events]
    event_ids = [str(event.event_id) for event in events]
    try:
        async with event_session_factory() as session:
            version_before = int(
                await session.scalar(text("SELECT data_version FROM data_state WHERE id = 1"))
            )
        results = await asyncio.gather(*(create(event) for event in events), return_exceptions=True)
        assert all(not isinstance(result, BaseException) and result.status == "processed" for result in results), results
        async with event_session_factory() as session:
            rows = (await session.execute(select(Order.id).where(Order.order_no.in_(order_ids)))).scalars().all()
            assert len(rows) == len(events)
            assert len(set(rows)) == len(events)
            version_after = int(
                await session.scalar(text("SELECT data_version FROM data_state WHERE id = 1"))
            )
        # 并发事件按事务提交顺序递增版本，且每笔成功事件恰好 +1。
        assert version_after == version_before + len(events)
    finally:
        async with event_session_factory() as session:
            async with session.begin():
                await session.execute(delete(OrderEvent).where(OrderEvent.event_id.in_(event_ids)))
                await session.execute(delete(Order).where(Order.order_no.in_(order_ids)))
        # 在测试事件循环仍有效时释放连接，避免退出时 aiomysql 清理已关闭的循环。
        await event_engine.dispose()


def test_sql_bootstrap_defines_auto_increment_pk():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[2] / "sql" / "01_create_table.sql").read_text(encoding="utf-8")
    assert "order_seq_id INT NOT NULL AUTO_INCREMENT" in sql


def test_sales_trend_skips_null_period_rows():
    """回归：order_time 为 NULL 时 date_format 得到 NULL，不能进入 SalesTrendItem。"""
    from backend.models.schemas import SalesTrendItem

    rows = [
        type("R", (), {"period": "2025-01-01", "sales": 10, "order_count": 1})(),
        type("R", (), {"period": None, "sales": 5, "order_count": 2})(),
        type("R", (), {"period": "2025-01-02", "sales": 3, "order_count": 1})(),
    ]
    data = [
        SalesTrendItem(period=str(row.period), sales=round(row.sales, 2), order_count=row.order_count)
        for row in rows
        if row.period is not None
    ]
    assert [item.period for item in data] == ["2025-01-01", "2025-01-02"]
