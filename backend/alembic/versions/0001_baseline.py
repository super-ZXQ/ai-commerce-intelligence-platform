"""baseline: orders + order_events + app_metadata（与生产 MySQL 8 语义对齐）

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-18

与 sql/01_create_table.sql / sync_orders.app_metadata 对齐：
- 不使用 ORM autogenerate（复合索引与 ORM index=True 不一致）
- 空库：CREATE TABLE IF NOT EXISTS 一次到位
- 存量库：对缺失列做 information_schema 判断后的 ALTER（幂等升级）
- 不写入任何业务测试数据
"""
from __future__ import annotations

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

_ORDERS_COLUMNS: list[tuple[str, str]] = [
    ("order_seq_id", "order_seq_id INT NOT NULL AUTO_INCREMENT"),
    ("order_id", "order_id VARCHAR(50) NOT NULL"),
    ("user_name", "user_name VARCHAR(100) NULL"),
    ("product_id", "product_id VARCHAR(50) NULL"),
    ("order_amount", "order_amount DECIMAL(18, 2) NULL"),
    ("payment_amount", "payment_amount DECIMAL(18, 2) NULL"),
    ("channel_id", "channel_id VARCHAR(50) NULL"),
    ("platform_type", "platform_type VARCHAR(50) NULL"),
    ("order_time", "order_time DATETIME NULL"),
    ("payment_time", "payment_time DATETIME NULL"),
    ("is_refund", "is_refund VARCHAR(10) NULL"),
    ("discount_amount", "discount_amount DECIMAL(18, 2) NULL"),
    ("payment_duration_sec", "payment_duration_sec INT NULL"),
    ("order_date", "order_date DATE NULL"),
    ("order_hour", "order_hour TINYINT UNSIGNED NULL"),
    ("weekday", "weekday VARCHAR(20) NULL"),
    ("order_status", "order_status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'"),
    ("event_version", "event_version INT NOT NULL DEFAULT 0"),
    ("updated_at", "updated_at DATETIME NULL"),
]

_EVENT_COLUMNS: list[tuple[str, str]] = [
    ("id", "id INT NOT NULL AUTO_INCREMENT"),
    ("event_id", "event_id VARCHAR(64) NOT NULL"),
    ("order_id", "order_id VARCHAR(50) NOT NULL"),
    ("event_type", "event_type VARCHAR(32) NOT NULL"),
    ("occurred_at", "occurred_at DATETIME NOT NULL"),
    ("sequence", "sequence INT NOT NULL"),
    ("source", "source VARCHAR(64) NOT NULL"),
    ("payload", "payload JSON NOT NULL"),
    ("processing_status", "processing_status VARCHAR(20) NOT NULL"),
    ("processed_at", "processed_at DATETIME NULL"),
    ("failure_reason", "failure_reason TEXT NULL"),
]


def _column_exists(bind, table: str, column: str) -> bool:
    return bool(
        bind.exec_driver_sql(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (table, column),
        ).scalar()
    )


def _ensure_columns(bind, table: str, columns: list[tuple[str, str]]) -> None:
    for name, ddl in columns:
        if name == "order_seq_id":
            continue  # PK/AUTO_INCREMENT 在 CREATE 中处理；存量库用 migrate_event_schema
        if not _column_exists(bind, table, name):
            # 去掉列名前缀重复：ddl 形如 "order_status VARCHAR..."
            bind.exec_driver_sql(f"ALTER TABLE `{table}` ADD COLUMN {ddl}")


_ORDERS_INDEXES: list[tuple[str, str]] = [
    # (index_name, create_sql)；空库 CREATE TABLE 已带齐，存量库按名补齐
    ("uq_orders_order_id", "UNIQUE INDEX `uq_orders_order_id` (`order_id`)"),
    ("idx_orders_order_time", "INDEX `idx_orders_order_time` (`order_time`)"),
    ("idx_orders_order_date", "INDEX `idx_orders_order_date` (`order_date`)"),
    ("idx_orders_platform_date", "INDEX `idx_orders_platform_date` (`platform_type`,`order_date`)"),
    ("idx_orders_user_date", "INDEX `idx_orders_user_date` (`user_name`,`order_date`)"),
    ("idx_orders_product_id", "INDEX `idx_orders_product_id` (`product_id`)"),
    ("idx_orders_payment_amount", "INDEX `idx_orders_payment_amount` (`payment_amount`)"),
    ("idx_orders_is_refund", "INDEX `idx_orders_is_refund` (`is_refund`)"),
    ("idx_orders_status_date", "INDEX `idx_orders_status_date` (`order_status`,`order_date`)"),
]

_EVENT_INDEXES: list[tuple[str, str]] = [
    ("uq_order_events_event_id", "UNIQUE INDEX `uq_order_events_event_id` (`event_id`)"),
    ("idx_order_events_order_sequence", "INDEX `idx_order_events_order_sequence` (`order_id`,`sequence`)"),
    ("idx_order_events_status_occurred", "INDEX `idx_order_events_status_occurred` (`processing_status`,`occurred_at`)"),
]


def _index_exists(bind, table: str, index_name: str) -> bool:
    return bool(
        bind.exec_driver_sql(
            "SELECT COUNT(*) FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s",
            (table, index_name),
        ).scalar()
    )


def _column_has_unique_index(bind, table: str, column: str) -> bool:
    return bool(
        bind.exec_driver_sql(
            "SELECT COUNT(*) FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND COLUMN_NAME = %s AND NON_UNIQUE = 0",
            (table, column),
        ).scalar()
    )


def _ensure_indexes(bind, table: str, indexes: list[tuple[str, str]]) -> None:
    for name, ddl in indexes:
        if _index_exists(bind, table, name):
            continue
        # 唯一索引：若同列已有唯一约束（名称不同），避免重复创建失败
        if name.startswith("uq_"):
            col = "order_id" if "order_id" in name else "event_id"
            if "order_events" in name:
                col = "event_id"
            if name == "uq_orders_order_id":
                col = "order_id"
            if _column_has_unique_index(bind, table, col):
                continue
        try:
            bind.exec_driver_sql(f"ALTER TABLE `{table}` ADD {ddl}")
        except Exception:  # noqa: BLE001 - 幂等：索引可能已以其他名称存在
            pass


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_seq_id INT NOT NULL AUTO_INCREMENT,
            order_id VARCHAR(50) NOT NULL,
            user_name VARCHAR(100) NULL,
            product_id VARCHAR(50) NULL,
            order_amount DECIMAL(18, 2) NULL,
            payment_amount DECIMAL(18, 2) NULL,
            channel_id VARCHAR(50) NULL,
            platform_type VARCHAR(50) NULL,
            order_time DATETIME NULL,
            payment_time DATETIME NULL,
            is_refund VARCHAR(10) NULL,
            discount_amount DECIMAL(18, 2) NULL,
            payment_duration_sec INT NULL,
            order_date DATE NULL,
            order_hour TINYINT UNSIGNED NULL,
            weekday VARCHAR(20) NULL,
            order_status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
            event_version INT NOT NULL DEFAULT 0,
            updated_at DATETIME NULL,
            PRIMARY KEY (order_seq_id),
            UNIQUE KEY uq_orders_order_id (order_id),
            KEY idx_orders_order_time (order_time),
            KEY idx_orders_order_date (order_date),
            KEY idx_orders_platform_date (platform_type, order_date),
            KEY idx_orders_user_date (user_name, order_date),
            KEY idx_orders_product_id (product_id),
            KEY idx_orders_payment_amount (payment_amount),
            KEY idx_orders_is_refund (is_refund),
            KEY idx_orders_status_date (order_status, order_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS order_events (
            id INT NOT NULL AUTO_INCREMENT,
            event_id VARCHAR(64) NOT NULL,
            order_id VARCHAR(50) NOT NULL,
            event_type VARCHAR(32) NOT NULL,
            occurred_at DATETIME NOT NULL,
            sequence INT NOT NULL,
            source VARCHAR(64) NOT NULL,
            payload JSON NOT NULL,
            processing_status VARCHAR(20) NOT NULL,
            processed_at DATETIME NULL,
            failure_reason TEXT NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uq_order_events_event_id (event_id),
            KEY idx_order_events_order_sequence (order_id, sequence),
            KEY idx_order_events_status_occurred (processing_status, occurred_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_metadata (
            meta_key VARCHAR(100) PRIMARY KEY,
            meta_value VARCHAR(255) NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    # 存量库幂等补齐字段与索引（空库 CREATE 已包含，此处为 no-op）
    _ensure_columns(bind, "orders", _ORDERS_COLUMNS)
    _ensure_columns(bind, "order_events", _EVENT_COLUMNS)
    _ensure_indexes(bind, "orders", _ORDERS_INDEXES)
    _ensure_indexes(bind, "order_events", _EVENT_INDEXES)


def downgrade() -> None:
    # baseline 回滚会丢业务表；仅允许在空库演练中使用。
    op.execute("DROP TABLE IF EXISTS app_metadata")
    op.execute("DROP TABLE IF EXISTS order_events")
    op.execute("DROP TABLE IF EXISTS orders")
