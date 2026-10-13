"""真实MySQL独立schema验收：并发、迁移、锁超时、回滚；绝不使用业务表。"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config import get_settings
from backend.models.database_models import Order, OrderEvent
from backend.models.schemas import OrderEventInput
from backend.services import order_event_service as service


@pytest.fixture(scope="module")
def mysql_audit_url():
    """独立随机schema，只清理本fixture创建的库；无法连库即测试失败。"""
    admin = create_engine(get_settings().database_url, hide_parameters=True)
    schema = "audit_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f"CREATE DATABASE `{schema}`"))
    url = admin.url.set(database=schema)
    cfg = Config("backend/alembic.ini")
    cfg.attributes["migration_url"] = url.render_as_string(hide_password=False)
    try:
        command.upgrade(cfg, "0001_baseline")
        old = create_engine(url)
        with old.begin() as connection:
            connection.execute(text("ALTER TABLE orders MODIFY order_seq_id INT NOT NULL"))
            connection.execute(text("INSERT INTO orders(order_seq_id,order_id) VALUES(5000,'historical')"))
        old.dispose()
        command.upgrade(cfg, "head")
        yield url
    finally:
        with admin.begin() as connection:
            connection.execute(text(f"DROP DATABASE `{schema}`"))
        admin.dispose()


@pytest.fixture
async def sessions(mysql_audit_url):
    engine = create_async_engine(
        mysql_audit_url.set(drivername="mysql+aiomysql"), isolation_level="READ COMMITTED",
        pool_size=20, max_overflow=80, hide_parameters=True,
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def event(order_id=None, kind="ORDER_CREATED", version=1):
    return OrderEventInput(
        event_id=str(uuid4()), order_id=order_id or uuid4().hex, event_type=kind,
        occurred_at=datetime.now(UTC), version=version, source="audit",
        payload={"user_name": "audit-user", "product_id": "P1", "order_amount": "10", "payment_amount": "10", "platform_type": "APP"}
        if kind == "ORDER_CREATED" else {},
    )


async def apply(sessions, item):
    async with sessions() as db:
        return await service.apply_order_event(db, item)


@pytest.mark.parametrize("size", [50, 100])
async def test_distinct_order_creates_have_unique_database_ids(sessions, size):
    events = [event() for _ in range(size)]
    results = await asyncio.gather(*(apply(sessions, item) for item in events))
    assert all(result.status == "processed" for result in results)
    async with sessions() as db:
        rows = (await db.scalars(select(Order).where(Order.order_no.in_([e.order_id for e in events])))).all()
        assert len(rows) == size
        assert len({row.id for row in rows}) == size
        assert min(row.id for row in rows) > 5000
        assert len((await db.scalars(select(OrderEvent).where(OrderEvent.order_no.in_([e.order_id for e in events])))).all()) == size


async def test_repeated_event_id_applies_once(sessions):
    item = event()
    results = await asyncio.gather(*(apply(sessions, item) for _ in range(20)))
    assert [r.status for r in results].count("processed") == 1
    assert [r.status for r in results].count("duplicate") == 19


async def test_terminal_state_competition_is_serialized(sessions):
    created = event()
    await apply(sessions, created)
    results = await asyncio.gather(
        apply(sessions, event(created.order_id, "ORDER_REFUNDED", 2)),
        apply(sessions, event(created.order_id, "ORDER_CANCELLED", 3)),
    )
    assert sorted(r.status for r in results) == ["processed", "rejected"]
    async with sessions() as db:
        order = await db.scalar(select(Order).where(Order.order_no == created.order_id))
        assert order.order_status in {"REFUNDED", "CANCELLED"}


async def test_lock_timeout_rolls_back_ledger_and_allows_retry(sessions):
    created = event()
    await apply(sessions, created)
    refund = event(created.order_id, "ORDER_REFUNDED", 2)
    async with sessions() as locker, sessions() as writer:
        async with locker.begin():
            await locker.execute(select(Order).where(Order.order_no == created.order_id).with_for_update())
            await writer.execute(text("SET SESSION innodb_lock_wait_timeout=1"))
            await writer.commit()
            with pytest.raises(OperationalError):
                await service.apply_order_event(writer, refund)
        assert (await service.apply_order_event(writer, refund)).status == "processed"


async def test_projection_failure_rolls_back_both_tables(sessions, monkeypatch):
    item = event()
    original = service._new_order

    def broken(*a, **k):
        raise RuntimeError("故障注入")

    monkeypatch.setattr(service, "_new_order", broken)
    with pytest.raises(RuntimeError):
        await apply(sessions, item)
    async with sessions() as db:
        assert await db.scalar(select(OrderEvent).where(OrderEvent.event_id == str(item.event_id))) is None
        assert await db.scalar(select(Order).where(Order.order_no == item.order_id)) is None
    monkeypatch.setattr(service, "_new_order", original)
    assert (await apply(sessions, item)).status == "processed"
