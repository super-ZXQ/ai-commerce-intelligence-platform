# 核心指标 SQL 公式集 (KPI Formulas)

> 与 `business_glossary.md` 配合使用
> 适用：直接复用或参考结构

## 1. 销售额相关

### 1.1 总销售额
```sql
SELECT SUM(payment_amount) AS total_sales
FROM orders
WHERE payment_amount > 0
  AND order_date BETWEEN '2025-01-01' AND '2025-12-31'
```

### 1.2 日销售额趋势
```sql
SELECT order_date,
       SUM(payment_amount) AS daily_sales,
       COUNT(DISTINCT order_no) AS daily_orders
FROM orders
WHERE payment_amount > 0
GROUP BY order_date
ORDER BY order_date
```

### 1.3 月销售额对比
```sql
SELECT DATE_FORMAT(order_date, '%Y-%m') AS month,
       SUM(payment_amount) AS monthly_sales
FROM orders
WHERE payment_amount > 0
GROUP BY month
ORDER BY month
```

## 2. 用户相关

### 2.1 总用户数 / 活跃用户
```sql
SELECT COUNT(DISTINCT user_name) AS active_users
FROM orders
WHERE payment_amount > 0
  AND order_date BETWEEN '2025-01-01' AND '2025-12-31'
```

### 2.2 复购率
```sql
WITH user_orders AS (
  SELECT user_name, COUNT(DISTINCT order_no) AS cnt
  FROM orders
  WHERE payment_amount > 0
  GROUP BY user_name
)
SELECT 
  SUM(CASE WHEN cnt >= 2 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS repurchase_rate
FROM user_orders
```

### 2.3 用户复购频次分布
```sql
SELECT 
  CASE 
    WHEN cnt = 1 THEN '1次(新客)'
    WHEN cnt BETWEEN 2 AND 3 THEN '2-3次'
    WHEN cnt BETWEEN 4 AND 10 THEN '4-10次'
    ELSE '10次以上(忠实)'
  END AS freq_band,
  COUNT(*) AS user_count
FROM (
  SELECT user_name, COUNT(DISTINCT order_no) AS cnt
  FROM orders WHERE payment_amount > 0
  GROUP BY user_name
) t
GROUP BY freq_band
```

## 3. 客单价 / 订单相关

### 3.1 客单价
```sql
SELECT SUM(payment_amount) / COUNT(DISTINCT order_no) AS avg_order_value
FROM orders
WHERE payment_amount > 0
  AND order_date BETWEEN '2025-01-01' AND '2025-12-31'
```

### 3.2 客单价区间分布
```sql
SELECT
  CASE
    WHEN payment_amount < 50  THEN '0-50元'
    WHEN payment_amount < 200 THEN '50-200元'
    WHEN payment_amount < 500 THEN '200-500元'
    WHEN payment_amount < 1500 THEN '500-1500元'
    ELSE '1500元以上'
  END AS price_band,
  COUNT(DISTINCT order_no) AS order_count
FROM orders
WHERE payment_amount > 0
GROUP BY price_band
```

## 4. 退款相关

### 4.1 退款率
```sql
SELECT 
  SUM(CASE WHEN is_refunded = '是' THEN 1 ELSE 0 END) * 100.0
  / COUNT(*) AS refund_rate
FROM orders
```

### 4.2 各平台退款率
```sql
SELECT platform_type,
       COUNT(*) AS total_orders,
       SUM(CASE WHEN is_refunded = '是' THEN 1 ELSE 0 END) AS refund_orders,
       SUM(CASE WHEN is_refunded = '是' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS refund_rate
FROM orders
GROUP BY platform_type
```

### 4.3 退款金额占比
```sql
SELECT 
  SUM(CASE WHEN is_refunded = '是' THEN payment_amount ELSE 0 END)
  / NULLIF(SUM(payment_amount), 0) AS refund_amount_ratio
FROM orders
WHERE payment_amount > 0
```

## 5. 平台/渠道分析

### 5.1 各平台销售占比
```sql
SELECT platform_type,
       SUM(payment_amount) AS sales,
       SUM(payment_amount) * 100.0 / (SELECT SUM(payment_amount) FROM orders WHERE payment_amount > 0) AS pct
FROM orders
WHERE payment_amount > 0
GROUP BY platform_type
ORDER BY sales DESC
```

### 5.2 平台客单价对比
```sql
SELECT platform_type,
       SUM(payment_amount) / COUNT(DISTINCT order_no) AS aov,
       COUNT(DISTINCT order_no) AS orders
FROM orders
WHERE payment_amount > 0
GROUP BY platform_type
```

## 6. 时间分布

### 6.1 24 小时销售分布
```sql
SELECT order_hour,
       SUM(payment_amount) AS sales,
       COUNT(DISTINCT order_no) AS orders
FROM orders
WHERE payment_amount > 0
GROUP BY order_hour
ORDER BY order_hour
```

### 6.2 星期销售分布
```sql
SELECT weekday,
       SUM(payment_amount) AS sales,
       COUNT(DISTINCT order_no) AS orders
FROM orders
WHERE payment_amount > 0
GROUP BY weekday
ORDER BY weekday
```

## 7. TOP N

### 7.1 销售额 TOP 10 用户
```sql
SELECT user_name, SUM(payment_amount) AS total_spent
FROM orders
WHERE payment_amount > 0
GROUP BY user_name
ORDER BY total_spent DESC
LIMIT 10
```

### 7.2 订单数 TOP 10 用户
```sql
SELECT user_name, COUNT(DISTINCT order_no) AS order_count
FROM orders
WHERE payment_amount > 0
GROUP BY user_name
ORDER BY order_count DESC
LIMIT 10
```

## 8. RFM 分析（参考 BI 看板逻辑）

### 8.1 用户 RFM 三维
```sql
SELECT user_name,
       DATEDIFF('2025-12-31', MAX(order_date)) AS recency_days,
       COUNT(DISTINCT order_no) AS frequency,
       SUM(payment_amount) AS monetary
FROM orders
WHERE payment_amount > 0
GROUP BY user_name
```

### 8.2 RFM 分层（8 类）
```sql
-- 在 8.1 基础上加分位打分，再按 阈值 R<=30, F>=2, M>=1500 分层
-- 完整实现见 backend/services/rfm_service.py
```

## 9. 留存与转化

### 9.1 次日留存
```sql
WITH first_visit AS (
  SELECT user_name, MIN(order_date) AS first_date
  FROM orders
  WHERE payment_amount > 0
  GROUP BY user_name
),
day1_active AS (
  SELECT DISTINCT user_name, order_date
  FROM orders
  WHERE payment_amount > 0
)
SELECT 
  COUNT(DISTINCT fv.user_name) AS day0_users,
  COUNT(DISTINCT CASE 
    WHEN DATEDIFF(da.order_date, fv.first_date) = 1 
    THEN fv.user_name END) AS day1_retained
FROM first_visit fv
LEFT JOIN day1_active da ON fv.user_name = da.user_name
```

## 10. 同比环比

### 10.1 月度同比
```sql
SELECT
  DATE_FORMAT(order_date, '%Y-%m') AS month,
  SUM(payment_amount) AS sales,
  LAG(SUM(payment_amount)) OVER (ORDER BY DATE_FORMAT(order_date, '%Y-%m')) AS prev_month
FROM orders
WHERE payment_amount > 0
GROUP BY month
```
