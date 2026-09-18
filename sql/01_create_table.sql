-- AI Commerce Intelligence Platform - MySQL 8 初始化表结构
-- 由 docker-entrypoint-initdb.d 在首次创建数据卷时自动执行。

CREATE TABLE IF NOT EXISTS orders (
    -- 事件摄入会创建新订单；主键必须可由 MySQL 自增，否则 INSERT 不带 order_seq_id 会失败
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
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- 事件表是新增订单和订单状态变化的唯一写入入口；CSV 仅用于首次 bootstrap。
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
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
