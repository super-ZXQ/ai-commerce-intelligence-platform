from sqlalchemy import JSON, Column, Date, DateTime, Integer, Numeric, String, Text, func

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


class AuthUser(Base):
    """数据库用户目录；密码只保存 bcrypt 哈希。"""

    __tablename__ = "auth_users"

    id = Column("id", Integer, primary_key=True, autoincrement=True)
    username = Column("username", String(100), unique=True, nullable=False, index=True)
    password_hash = Column("password_hash", String(255), nullable=False)
    role = Column("role", String(20), nullable=False, default="viewer")
    scopes = Column("scopes", String(512), nullable=False, default="")
    is_active = Column("is_active", Integer, nullable=False, default=1)
    token_version = Column("token_version", Integer, nullable=False, default=1)
    created_at = Column("created_at", DateTime, nullable=False, server_default=func.now())
    updated_at = Column("updated_at", DateTime, nullable=False, server_default=func.now())
    last_login_at = Column("last_login_at", DateTime, nullable=True)


class ServiceClient(Base):
    """机器客户端凭据；只保存密钥哈希，创建时明文仅输出一次。"""

    __tablename__ = "service_clients"

    id = Column("id", Integer, primary_key=True, autoincrement=True)
    name = Column("name", String(100), unique=True, nullable=False, index=True)
    key_prefix = Column("key_prefix", String(16), nullable=False, index=True)
    key_hash = Column("key_hash", String(64), unique=True, nullable=False)
    scopes = Column("scopes", String(512), nullable=False, default="")
    is_active = Column("is_active", Integer, nullable=False, default=1)
    created_at = Column("created_at", DateTime, nullable=False, server_default=func.now())
    last_used_at = Column("last_used_at", DateTime, nullable=True)
    expires_at = Column("expires_at", DateTime, nullable=True)


class AuthAuditLog(Base):
    __tablename__ = "auth_audit_log"

    id = Column("id", Integer, primary_key=True, autoincrement=True)
    principal_type = Column("principal_type", String(20), nullable=False)
    principal_name = Column("principal_name", String(100), nullable=False, index=True)
    action = Column("action", String(50), nullable=False)
    resource = Column("resource", String(255), nullable=False, default="")
    outcome = Column("outcome", String(20), nullable=False)
    client_ip = Column("client_ip", String(64), nullable=False, default="")
    detail = Column("detail", String(500), nullable=False, default="")
    created_at = Column("created_at", DateTime, nullable=False, server_default=func.now())


class AIQueryAudit(Base):
    """AI 查询审计：不保存原始问题，只保存哈希、长度、意图与用量。"""

    __tablename__ = "ai_query_audit"

    id = Column("id", Integer, primary_key=True, autoincrement=True)
    request_id = Column("request_id", String(64), nullable=False, index=True)
    principal_name = Column("principal_name", String(100), nullable=False, index=True)
    thread_id = Column("thread_id", String(128), nullable=False, default="")
    model_mode = Column("model_mode", String(20), nullable=False, default="managed")
    intent = Column("intent", String(32), nullable=False, default="")
    outcome = Column("outcome", String(20), nullable=False)
    query_sha256 = Column("query_sha256", String(64), nullable=False)
    query_length = Column("query_length", Integer, nullable=False, default=0)
    input_tokens = Column("input_tokens", Integer, nullable=True)
    output_tokens = Column("output_tokens", Integer, nullable=True)
    total_tokens = Column("total_tokens", Integer, nullable=True)
    latency_ms = Column("latency_ms", Integer, nullable=True)
    error_category = Column("error_category", String(80), nullable=False, default="")
    created_at = Column("created_at", DateTime, nullable=False, server_default=func.now())


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
