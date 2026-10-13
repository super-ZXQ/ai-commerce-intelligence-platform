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


async def test_api_ai_and_bi_effective_metrics_agree_after_events(sessions, mysql_audit_url):
    from agent_core.business_data import describe_data_range, load_live_orders
    from agent_core.sql_safety import validate_and_limit_sql
    from backend.services.analytics_service import get_sales_overview

    sync_engine = create_engine(mysql_audit_url)
    items = [event() for _ in range(3)]
    try:
        for item in items:
            await apply(sessions, item)
        for step in (None, "ORDER_REFUNDED", "ORDER_CANCELLED"):
            if step:
                await apply(sessions, event(items[1 if step == "ORDER_REFUNDED" else 2].order_id, step, 2))
            async with sessions() as db:
                api = await get_sales_overview.__wrapped__(db)
                sql = validate_and_limit_sql(
                    "SELECT COALESCE(SUM(payment_amount),0), COUNT(DISTINCT order_id) "
                    "FROM orders WHERE order_status='ACTIVE' AND COALESCE(is_refund,'否')<>'是'"
                )
                ai_sales, ai_orders = (await db.execute(text(sql))).one()
            frame = load_live_orders(sync_engine)
            assert api.total_sales == ai_sales == frame["付款金额"].sum()
            assert api.total_orders == ai_orders == frame["订单号"].nunique()
        assert "2026-" in describe_data_range(sync_engine)
    finally:
        sync_engine.dispose()


def test_ai_account_actual_grants_allow_aggregates_and_deny_writes(mysql_audit_url):
    import secrets
    from sqlalchemy.exc import DBAPIError
    from agent_core.db_permissions import verify_ai_permissions
    from agent_core.sql_safety import ORDER_COLUMNS

    admin = create_engine(mysql_audit_url, hide_parameters=True)
    user, password = "audit_" + uuid4().hex[:20], secrets.token_urlsafe(24)
    reader = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f"CREATE USER '{user}'@'%' IDENTIFIED BY :password"), {"password": password})
            fields = ",".join(sorted(ORDER_COLUMNS))
            connection.execute(text(f"GRANT SELECT ({fields}) ON `{mysql_audit_url.database}`.orders TO '{user}'@'%'"))
        reader = create_engine(mysql_audit_url.set(username=user, password=password), hide_parameters=True)
        verify_ai_permissions(reader)
        with reader.connect() as connection:
            assert connection.execute(text("SELECT COUNT(DISTINCT order_id),SUM(payment_amount) FROM orders")).one()
            for sql in ("UPDATE orders SET payment_amount=0", "SELECT payload FROM order_events", "DELETE FROM orders", "CREATE TABLE denied(id INT)"):
                with pytest.raises(DBAPIError):
                    connection.execute(text(sql))
                connection.rollback()
        with admin.begin() as connection:
            connection.execute(text(f"GRANT UPDATE ON `{mysql_audit_url.database}`.orders TO '{user}'@'%'"))
        with pytest.raises(ValueError):
            verify_ai_permissions(reader)
    finally:
        if reader is not None:
            reader.dispose()
        with admin.begin() as connection:
            connection.execute(text(f"DROP USER IF EXISTS '{user}'@'%'"))
        admin.dispose()
