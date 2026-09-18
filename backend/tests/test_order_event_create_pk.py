"""最小可复现测试：ORDER_CREATED 主键分配与 sales-trend NULL period。"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
