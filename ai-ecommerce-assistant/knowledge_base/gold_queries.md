# 黄金查询集 (Gold Queries)

> 高频业务问题 → 标准 SQL
> 当用户问题匹配时优先复用

## 销售类

### GQ-001: 今天 / 当前总销售额
```sql
SELECT SUM(payment_amount) AS total_sales
FROM orders
WHERE payment_amount > 0
```
> 回答: "平台总销售额 X 元"

### GQ-002: 最近 7 天销售趋势
```sql
SELECT order_date, SUM(payment_amount) AS sales
FROM orders
WHERE payment_amount > 0
  AND order_date >= DATE_SUB((SELECT MAX(order_date) FROM orders), INTERVAL 7 DAY)
GROUP BY order_date
ORDER BY order_date
```

### GQ-003: 各平台销售额对比
```sql
SELECT platform_type, SUM(payment_amount) AS sales
FROM orders
WHERE payment_amount > 0
GROUP BY platform_type
ORDER BY sales DESC
```

### GQ-004: 哪个平台销售额最高
```sql
SELECT platform_type, SUM(payment_amount) AS sales
FROM orders
WHERE payment_amount > 0
GROUP BY platform_type
ORDER BY sales DESC
LIMIT 1
```

### GQ-005: 日均销售额
```sql
SELECT SUM(payment_amount) / COUNT(DISTINCT order_date) AS daily_avg
FROM orders
WHERE payment_amount > 0
```

## 用户类

### GQ-006: 总活跃用户数
```sql
SELECT COUNT(DISTINCT user_name) AS active_users
FROM orders
WHERE payment_amount > 0
```

### GQ-007: 复购率
```sql
WITH t AS (
  SELECT user_name, COUNT(DISTINCT order_no) AS cnt
  FROM orders WHERE payment_amount > 0
  GROUP BY user_name
)
SELECT SUM(CASE WHEN cnt >= 2 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS repurchase_rate
FROM t
```

### GQ-008: 消费 TOP 10 用户
```sql
SELECT user_name, SUM(payment_amount) AS total_spent
FROM orders WHERE payment_amount > 0
GROUP BY user_name
ORDER BY total_spent DESC
LIMIT 10
```

### GQ-009: 消费 1 次的新客数
```sql
SELECT COUNT(*) AS new_user_count
FROM (
  SELECT user_name, COUNT(DISTINCT order_no) AS cnt
  FROM orders WHERE payment_amount > 0
  GROUP BY user_name
  HAVING cnt = 1
) t
```

## 退款类

### GQ-010: 总退款率
```sql
SELECT 
  SUM(CASE WHEN is_refunded = '是' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS refund_rate
FROM orders
```

### GQ-011: 哪个平台退款率最高
```sql
SELECT platform_type,
       SUM(CASE WHEN is_refunded = '是' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS rate
FROM orders
GROUP BY platform_type
ORDER BY rate DESC
LIMIT 1
```

### GQ-012: 退款订单数
```sql
SELECT COUNT(DISTINCT order_no) AS refund_orders
FROM orders
WHERE is_refunded = '是'
```

## 客单价类

### GQ-013: 平台总客单价
```sql
SELECT SUM(payment_amount) / COUNT(DISTINCT order_no) AS aov
FROM orders
WHERE payment_amount > 0
```

### GQ-014: 客单价 TOP 10 用户
```sql
SELECT user_name, 
       SUM(payment_amount) / COUNT(DISTINCT order_no) AS user_aov
FROM orders
WHERE payment_amount > 0
GROUP BY user_name
ORDER BY user_aov DESC
LIMIT 10
```

## 时段类

### GQ-015: 销售高峰时段（TOP 3 小时）
```sql
SELECT order_hour, SUM(payment_amount) AS sales
FROM orders
WHERE payment_amount > 0
GROUP BY order_hour
ORDER BY sales DESC
LIMIT 3
```

### GQ-016: 工作日 vs 周末销售
```sql
SELECT 
  CASE WHEN weekday IN (5, 6) THEN '周末' ELSE '工作日' END AS day_type,
  SUM(payment_amount) AS sales
FROM orders
WHERE payment_amount > 0
GROUP BY day_type
```

## 订单类

### GQ-017: 总订单数
```sql
SELECT COUNT(DISTINCT order_no) AS total_orders
FROM orders
WHERE payment_amount > 0
```

### GQ-018: 日均订单数
```sql
SELECT COUNT(DISTINCT order_no) / COUNT(DISTINCT order_date) AS daily_avg_orders
FROM orders
WHERE payment_amount > 0
```

## 占比类

### GQ-019: APP 渠道销售占比
```sql
SELECT 
  SUM(CASE WHEN platform_type = 'APP' THEN payment_amount ELSE 0 END)
  / NULLIF(SUM(payment_amount), 0) AS app_share
FROM orders
WHERE payment_amount > 0
```

### GQ-020: 退款金额占总销售比例
```sql
SELECT 
  SUM(CASE WHEN is_refunded = '是' THEN payment_amount ELSE 0 END)
  / NULLIF(SUM(payment_amount), 0) AS refund_amount_share
FROM orders
WHERE payment_amount > 0
```

## RFM 类

### GQ-021: 重要价值客户数
```sql
-- 需先计算 RFM，参考 kpi_formulas.md 8.1 和 8.2
-- 通常在 backend/services/rfm_service.py 中实现
```

### GQ-022: 高价值客户销售额贡献
```sql
-- 同上，RFM 分层后聚合
```

## 同比环比

### GQ-023: 月度销售趋势
```sql
SELECT DATE_FORMAT(order_date, '%Y-%m') AS month,
       SUM(payment_amount) AS sales
FROM orders
WHERE payment_amount > 0
GROUP BY month
ORDER BY month
```

### GQ-024: 上个月 vs 本月
```sql
SELECT
  SUM(CASE WHEN order_date >= '2025-12-01' THEN payment_amount ELSE 0 END) AS this_month,
  SUM(CASE WHEN order_date >= '2025-11-01' AND order_date < '2025-12-01' THEN payment_amount ELSE 0 END) AS last_month
FROM orders
WHERE payment_amount > 0
```

## 留存类

### GQ-025: 月度新客数
```sql
SELECT DATE_FORMAT(first_date, '%Y-%m') AS month, COUNT(*) AS new_users
FROM (
  SELECT user_name, MIN(order_date) AS first_date
  FROM orders WHERE payment_amount > 0
  GROUP BY user_name
) t
GROUP BY month
ORDER BY month
```

### GQ-026: 用户首次消费距今天数分布
```sql
SELECT
  CASE 
    WHEN days_since <= 7 THEN '新客(≤7天)'
    WHEN days_since <= 30 THEN '近期(8-30天)'
    WHEN days_since <= 90 THEN '中期(31-90天)'
    ELSE '老客(>90天)'
  END AS tenure_band,
  COUNT(*) AS user_count
FROM (
  SELECT user_name, DATEDIFF('2025-12-31', MIN(order_date)) AS days_since
  FROM orders WHERE payment_amount > 0
  GROUP BY user_name
) t
GROUP BY tenure_band
```

## 商品 / 类目类（商品表未引入前的占位）

### GQ-027: 订单金额分布
```sql
SELECT
  CASE 
    WHEN payment_amount < 100 THEN '<100元'
    WHEN payment_amount < 500 THEN '100-500元'
    WHEN payment_amount < 1000 THEN '500-1000元'
    WHEN payment_amount < 5000 THEN '1000-5000元'
    ELSE '≥5000元'
  END AS band,
  COUNT(DISTINCT order_no) AS cnt
FROM orders
WHERE payment_amount > 0
GROUP BY band
ORDER BY MIN(payment_amount)
```

## 异常检测类

### GQ-028: 当日销售额骤降检测
```sql
WITH daily AS (
  SELECT order_date, SUM(payment_amount) AS sales
  FROM orders WHERE payment_amount > 0
  GROUP BY order_date
)
SELECT order_date, sales,
  AVG(sales) OVER (ORDER BY order_date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS ma7
FROM daily
ORDER BY order_date
```

### GQ-029: 退款率异常平台
```sql
SELECT platform_type,
  SUM(CASE WHEN is_refunded = '是' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS rate
FROM orders
GROUP BY platform_type
HAVING rate > 12  -- 行业基准上限
```

## 行为类

### GQ-030: 同一用户首单与末单间隔
```sql
SELECT 
  AVG(span) AS avg_lifespan_days,
  MAX(span) AS max_lifespan_days
FROM (
  SELECT user_name,
    DATEDIFF(MAX(order_date), MIN(order_date)) AS span
  FROM orders
  WHERE payment_amount > 0
  GROUP BY user_name
  HAVING COUNT(DISTINCT order_no) >= 2
) t
```
