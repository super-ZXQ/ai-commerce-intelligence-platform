from sqlalchemy import JSON, Column, Date, DateTime, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects import mysql

from backend.database import Base

# 列型必须与已部署 DDL（backend/alembic/versions/0001-0006）逐一对齐：
# ORM 声明成 INT/NUMERIC(10,2) 时，任何走 create_all 的环境都会建出一个比生产更窄、
# 且丢失 UNSIGNED 约束的 schema，测试通过的写入可能在生产溢出。
# 漂移由 backend/tests/test_alembic_migrations.py 的 ORM↔schema 守卫拦住。


class Order(Base):
    __tablename__ = "orders"

    id = Column("order_seq_id", Integer, primary_key=True, autoincrement=True)
    order_no = Column("order_id", String(50), unique=True, nullable=False, index=True)
    user_name = Column("user_name", String(100), index=True)
    product_id = Column("product_id", String(50), index=True)
    order_amount = Column("order_amount", Numeric(18, 2))
    payment_amount = Column("payment_amount", Numeric(18, 2))
    channel_id = Column("channel_id", String(50))
    platform_type = Column("platform_type", String(50), index=True)
    order_time = Column("order_time", DateTime, index=True)
    payment_time = Column("payment_time", DateTime)
    is_refunded = Column("is_refund", String(10))
    discount_amount = Column("discount_amount", Numeric(18, 2))
    payment_duration_sec = Column("payment_duration_sec", Integer)
    order_date = Column("order_date", Date, index=True)
    order_hour = Column("order_hour", mysql.TINYINT(unsigned=True))
    weekday = Column("weekday", String(20))
    # 历史 CSV 行默认视为已经付款的有效订单；后续状态只能由事件流推进。
    order_status = Column("order_status", String(20), nullable=False, default="ACTIVE", index=True)
    event_version = Column("event_version", Integer, nullable=False, default=0)
    updated_at = Column("updated_at", DateTime, nullable=True)


class AuthUser(Base):
    """数据库用户目录；密码只保存 bcrypt 哈希。"""

    __tablename__ = "auth_users"

    id = Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    username = Column("username", String(100), unique=True, nullable=False, index=True)
    password_hash = Column("password_hash", String(255), nullable=False)
    role = Column("role", String(20), nullable=False, default="viewer")
    scopes = Column("scopes", String(512), nullable=False, default="")
    is_active = Column("is_active", mysql.TINYINT(1), nullable=False, default=1)
    token_version = Column("token_version", mysql.INTEGER(unsigned=True), nullable=False, default=1)
    created_at = Column("created_at", DateTime, nullable=False, server_default=func.now())
    updated_at = Column("updated_at", DateTime, nullable=False, server_default=func.now())
    last_login_at = Column("last_login_at", DateTime, nullable=True)


class ServiceClient(Base):
    """机器客户端凭据；只保存密钥哈希，创建时明文仅输出一次。"""

    __tablename__ = "service_clients"

    id = Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    name = Column("name", String(100), unique=True, nullable=False, index=True)
    key_prefix = Column("key_prefix", String(16), nullable=False, index=True)
    key_hash = Column("key_hash", mysql.CHAR(64), unique=True, nullable=False)
    scopes = Column("scopes", String(512), nullable=False, default="")
    is_active = Column("is_active", mysql.TINYINT(1), nullable=False, default=1)
    created_at = Column("created_at", DateTime, nullable=False, server_default=func.now())
    last_used_at = Column("last_used_at", DateTime, nullable=True)
    expires_at = Column("expires_at", DateTime, nullable=True)


class AuthAuditLog(Base):
    __tablename__ = "auth_audit_log"

    id = Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
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

    id = Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    request_id = Column("request_id", String(64), nullable=False, index=True)
    principal_name = Column("principal_name", String(100), nullable=False, index=True)
    thread_id = Column("thread_id", String(128), nullable=False, default="")
    model_mode = Column("model_mode", String(20), nullable=False, default="managed")
    intent = Column("intent", String(32), nullable=False, default="")
    outcome = Column("outcome", String(20), nullable=False)
    query_sha256 = Column("query_sha256", mysql.CHAR(64), nullable=False)
    query_length = Column("query_length", mysql.INTEGER(unsigned=True), nullable=False, default=0)
    input_tokens = Column("input_tokens", mysql.INTEGER(unsigned=True), nullable=True)
    output_tokens = Column("output_tokens", mysql.INTEGER(unsigned=True), nullable=True)
    total_tokens = Column("total_tokens", mysql.INTEGER(unsigned=True), nullable=True)
    latency_ms = Column("latency_ms", mysql.INTEGER(unsigned=True), nullable=True)
    error_category = Column("error_category", String(80), nullable=False, default="")
    created_at = Column("created_at", DateTime, nullable=False, server_default=func.now())


class AgentMemoryFact(Base):
    """按认证主体隔离的长期偏好；只允许白名单键值，不存原始对话。"""

    __tablename__ = "agent_memory_fact"
    __table_args__ = (
        UniqueConstraint("owner", "memory_key", name="uq_agent_memory_owner_key"),
    )

    id = Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    owner = Column("owner", String(100), nullable=False, index=True)
    memory_key = Column("memory_key", String(64), nullable=False)
    memory_value = Column("memory_value", String(255), nullable=False)
    confidence = Column("confidence", Numeric(3, 2), nullable=False, default=0.95)
    source_thread_id = Column("source_thread_id", String(128), nullable=False, default="")
    source_request_id = Column("source_request_id", String(64), nullable=False, default="")
    expires_at = Column("expires_at", DateTime, nullable=True)
    created_at = Column("created_at", DateTime, nullable=False, server_default=func.now())
    updated_at = Column("updated_at", DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class AgentMemoryEpisode(Base):
    """按主体隔离的历史情节摘要；embedding 可为空以支持关键词降级。"""

    __tablename__ = "agent_memory_episode"
    __table_args__ = (
        UniqueConstraint("owner", "source_request_id", name="uq_episode_owner_request"),
    )

    id = Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    owner = Column("owner", String(100), nullable=False, index=True)
    thread_id = Column("thread_id", String(128), nullable=False, default="")
    source_request_id = Column("source_request_id", String(64), nullable=False)
    summary = Column("summary", Text, nullable=False)
    intent = Column("intent", String(32), nullable=False, default="")
    embedding = Column("embedding", JSON, nullable=True)
    embedding_model = Column("embedding_model", String(64), nullable=False, default="")
    expires_at = Column("expires_at", DateTime, nullable=True)
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


class DataImportRun(Base):
    """业务上传导入的运行记录（草稿/确认/执行/审计共用一条记录）。"""

    __tablename__ = "data_import_runs"

    id = Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    source = Column("source", String(512), nullable=False)
    source_sha256 = Column("source_sha256", mysql.CHAR(64), nullable=False)
    mode = Column("mode", String(20), nullable=False)
    status = Column("status", String(20), nullable=False)
    operator = Column("operator", String(100), nullable=False)
    rows_read = Column("rows_read", mysql.INTEGER(unsigned=True), nullable=False, default=0)
    rows_imported = Column("rows_imported", mysql.INTEGER(unsigned=True), nullable=False, default=0)
    backup_table = Column("backup_table", String(64), nullable=True)
    started_at = Column("started_at", DateTime, nullable=False)
    finished_at = Column("finished_at", DateTime, nullable=True)
    error_message = Column("error_message", Text, nullable=True)
    notes = Column("notes", Text, nullable=True)
    source_type = Column("source_type", String(10), nullable=False, default="csv")
    original_filename = Column("original_filename", String(255), nullable=False, default="")
    source_size_bytes = Column(
        "source_size_bytes", mysql.BIGINT(unsigned=True), nullable=False, default=0
    )
    options_json = Column("options_json", JSON, nullable=True)
    conflict_policy = Column("conflict_policy", String(20), nullable=False, default="reject")
    rows_valid = Column("rows_valid", mysql.INTEGER(unsigned=True), nullable=False, default=0)
    rows_conflict = Column("rows_conflict", mysql.INTEGER(unsigned=True), nullable=False, default=0)
    rows_invalid = Column("rows_invalid", mysql.INTEGER(unsigned=True), nullable=False, default=0)
    rows_skipped = Column("rows_skipped", mysql.INTEGER(unsigned=True), nullable=False, default=0)
    rows_rejected = Column("rows_rejected", mysql.INTEGER(unsigned=True), nullable=False, default=0)
    rows_inserted = Column("rows_inserted", mysql.INTEGER(unsigned=True), nullable=False, default=0)
    rows_refunded = Column("rows_refunded", mysql.INTEGER(unsigned=True), nullable=False, default=0)
    data_version_before = Column(
        "data_version_before", mysql.INTEGER(unsigned=True), nullable=True
    )
    data_version_after = Column(
        "data_version_after", mysql.INTEGER(unsigned=True), nullable=True
    )
    confirmed_by = Column("confirmed_by", String(100), nullable=False, default="")
    confirmed_at = Column("confirmed_at", DateTime, nullable=True)
    confirm_token_hash = Column("confirm_token_hash", mysql.CHAR(64), nullable=False, default="")
    heartbeat_at = Column("heartbeat_at", DateTime, nullable=True)
    error_code = Column("error_code", String(64), nullable=False, default="")
    # 取消只设置标记：执行任务在当前分块提交后才读取，已提交部分不会被回滚。
    cancel_requested = Column(
        "cancel_requested", mysql.TINYINT(1), nullable=False, default=0
    )


class DataImportRow(Base):
    """上传草稿的逐行暂存与失败明细；确认后执行，过期由导入入口清理。"""

    __tablename__ = "data_import_rows"
    __table_args__ = (
        UniqueConstraint("run_id", "row_no", name="uq_data_import_rows_run_row"),
    )

    id = Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    run_id = Column("run_id", mysql.BIGINT(unsigned=True), nullable=False, index=True)
    row_no = Column("row_no", mysql.INTEGER(unsigned=True), nullable=False)
    order_id = Column("order_id", String(50), nullable=False, default="")
    status = Column("status", String(20), nullable=False, index=True)
    error_code = Column("error_code", String(64), nullable=False, default="")
    error_message = Column("error_message", String(500), nullable=False, default="")
    normalized_json = Column("normalized_json", JSON, nullable=True)
    raw_json = Column("raw_json", JSON, nullable=True)
    created_at = Column("created_at", DateTime, nullable=False, server_default=func.now())
