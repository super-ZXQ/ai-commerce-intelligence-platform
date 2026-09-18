"""合成事件流必须可重复，且不能把公开样本中的个人字段带入请求。"""

from scripts.order_event_producer import build_events


def test_event_producer_is_deterministic_for_same_seed():
    first = build_events(
        seed=7, count=5, refund_rate=0.4, duplicate_rate=0.4,
        out_of_order_rate=0.4, invalid_rate=0.2,
    )
    second = build_events(
        seed=7, count=5, refund_rate=0.4, duplicate_rate=0.4,
        out_of_order_rate=0.4, invalid_rate=0.2,
    )
    assert first == second


def test_event_producer_uses_synthetic_identity_only():
    events = build_events(
        seed=8, count=3, refund_rate=0, duplicate_rate=0,
        out_of_order_rate=0, invalid_rate=0,
    )
    created = [event for event in events if event["event_type"] == "ORDER_CREATED"]
    assert all(event["order_id"].startswith("SYN-") for event in created)
    assert all(event["payload"]["user_name"].startswith("synthetic_user_") for event in created)
