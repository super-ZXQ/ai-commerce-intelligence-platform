-- ============================================
-- 电商数据分析系统 - 数据库索引优化
-- 执行前请确保已连接到 ecommerce_analysis 数据库
-- ============================================

USE ecommerce_analysis;

-- 1. 查看当前索引
SHOW INDEX FROM orders;

-- 2. 复合索引（高频查询路径）
-- 订单列表：按时间排序 + 分页
ALTER TABLE orders ADD INDEX idx_order_time_desc (order_time DESC);

-- 筛选查询：平台类型 + 时间
ALTER TABLE orders ADD INDEX idx_platform_date (platform_type, order_date);

-- 用户行为分析：用户名 + 时间（复购率/活跃度）
ALTER TABLE orders ADD INDEX idx_user_orderdate (user_name, order_date);

-- 商品销售排名：商品编号
ALTER TABLE orders ADD INDEX idx_product_id (product_id);

-- 金额筛选：付款金额
ALTER TABLE orders ADD INDEX idx_payment_amount (payment_amount);

-- 退款分析
ALTER TABLE orders ADD INDEX idx_is_refunded (is_refunded);

-- 下单时间范围查询
ALTER TABLE orders ADD INDEX idx_order_date (order_date);

-- 3. 慢查询日志配置（临时生效，重启后失效）
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 1;
SET GLOBAL log_queries_not_using_indexes = 'ON';

-- 查看慢查询状态
SHOW VARIABLES LIKE 'slow_query%';
SHOW VARIABLES LIKE 'long_query_time';

-- 4. 分析表（更新统计信息）
ANALYZE TABLE orders;

-- 5. 验证索引
SHOW INDEX FROM orders;

-- 6. 查看表大小和索引使用情况
SELECT 
    table_name,
    table_rows,
    ROUND(data_length/1024/1024, 2) AS data_mb,
    ROUND(index_length/1024/1024, 2) AS index_mb
FROM information_schema.tables 
WHERE table_schema = 'ecommerce_analysis' AND table_name = 'orders';
