from sqlalchemy import JSON, Column, Date, DateTime, Integer, Numeric, String, Text

from backend.database import Base


class Order(Base):
    __tablename__ = "orders"

    id = Column("order_seq_id", Integer, primary_key=True, autoincrement=True)
    order_no = Column("order_id", String(50), unique=True, nullable=False, index=True)
    user_name = Column("user_name", String(100), index=True)
    product_id = Column("product_id", String(50), index=True)
    order_amount = Column("order_amount", Numeric(10, 2))
    payment_amount = Column("payment_amount", Numeric(10, 2))
    channel_id = Column("channel_id", String(50))
    platform_type = Column("platform_type", String(50), index=True)
    order_time = Column("order_time", DateTime, index=True)
    payment_time = Column("payment_time", DateTime)
    is_refunded = Column("is_refund", String(10))
    discount_amount = Column("discount_amount", Numeric(10, 2))
    payment_duration_sec = Column("payment_duration_sec", Integer)
    order_date = Column("order_date", Date, index=True)
    order_hour = Column("order_hour", Integer)
    weekday = Column("weekday", String(20))
    # 历史 CSV 行默认视为已经付款的有效订单；后续状态只能由事件流推进。
    order_status = Column("order_status", String(20), nullable=False, default="ACTIVE", index=True)
    event_version = Column("event_version", Integer, nullable=False, default=0)
    updated_at = Column("updated_at", DateTime, nullable=True)


class OrderEvent(Base):
    """订单事件账本。

    event_id 的唯一约束是幂等性的最终边界：即使多个 API 实例同时收到同一事件，
    数据库也只允许一个事务真正修改订单读模型。
    """

    __tablename__ = "order_events"

    id = Column("id", Integer, primary_key=True, autoincrement=True)
    event_id = Column("event_id", String(64), nullable=False, unique=True, index=True)
    order_no = Column("order_id", String(50), nullable=False, index=True)
    event_type = Column("event_type", String(32), nullable=False, index=True)
    occurred_at = Column("occurred_at", DateTime, nullable=False, index=True)
    sequence = Column("sequence", Integer, nullable=False)
    source = Column("source", String(64), nullable=False)
    payload = Column("payload", JSON, nullable=False)
    processing_status = Column("processing_status", String(20), nullable=False, index=True)
    processed_at = Column("processed_at", DateTime, nullable=True)
    failure_reason = Column("failure_reason", Text, nullable=True)
