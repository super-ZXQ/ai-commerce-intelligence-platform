from sqlalchemy import Column, Integer, String, Numeric, DateTime, Date
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
    order_date = Column("order_date", Date)
    order_hour = Column("order_hour", Integer)
    weekday = Column("weekday", String(20))
