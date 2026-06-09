# 数据字典 (Data Dictionary)

> 数据库：MySQL 8 / SQLite (本地开发)
> 主表：`orders`（订单事实表）
> 记录数：10万+
> 适用对象：AI 助手生成 SQL 时引用

## 表结构概览

| 表名 | 含义 | 主键 | 行数 |
|------|------|------|------|
| `orders` | 订单事实表 | `id` | ~100,000 |
| `users` (未来) | 用户维度表 | `user_id` | - |
| `products` (未来) | 商品维度表 | `product_id` | - |

## orders 表字段说明

### 基础标识字段

| 字段名 | 类型 | 含义 | 备注 |
|--------|------|------|------|
| `id` | INT | 主键 | 自增，业务不引用 |
| `order_no` | VARCHAR(64) | 订单号 | 业务唯一标识 |
| `user_name` | VARCHAR(64) | 用户名 | 脱敏后，可能重复 |

### 时间字段（3 个）

| 字段名 | 类型 | 含义 | 用途 |
|--------|------|------|------|
| `order_date` | DATE | 下单日期 | 时间范围筛选主字段 |
| `order_time` | DATETIME | 下单时间 | 精确时段分析 |
| `order_hour` | INT | 下单小时 (0-23) | 时段分布图 |
| `weekday` | INT | 星期几 (0-周一, 6-周日) | 周分布分析 |

**注意**：
- `order_date` 是 DATE 类型，比较时不需要时间部分
- `order_time` 是 DATETIME 类型，格式 `2025-12-31 14:30:00`
- 不要混用 `order_date` 和 `order_time` 做范围筛选，统一用 `order_date`

### 渠道字段

| 字段名 | 类型 | 含义 | 枚举值 |
|--------|------|------|--------|
| `platform_type` | VARCHAR(16) | 平台类型 | `APP`, `微信公众号`, `Web网站`, `其他` |

**SQL 用法**：
```sql
-- 筛选 APP
WHERE platform_type = 'APP'
-- 多选
WHERE platform_type IN ('APP', '微信公众号')
-- 分组
GROUP BY platform_type
```

### 金额字段

| 字段名 | 类型 | 含义 | 单位 | 范围 |
|--------|------|------|------|------|
| `payment_amount` | DECIMAL(10,2) | 实付金额 | 元 | 0.00 - 99999.99 |

**注意**：
- **使用 `payment_amount`，不是 `order_amount`**（后者可能含未付款）
- 筛选"有效销售"必须 `WHERE payment_amount > 0`
- 不要用 `payment_amount = 0` 查"无销售"

### 状态字段

| 字段名 | 类型 | 含义 | 枚举值 |
|--------|------|------|--------|
| `is_refunded` | VARCHAR(2) | 是否退款 | `'是'` = 已退款, `'否'` = 未退款 |

**SQL 用法**：
```sql
-- 退款订单
WHERE is_refunded = '是'
-- 未退款
WHERE is_refunded = '否'
```

**注意**：
- 是中文"是"和"否"，不是 0/1 或 true/false
- 字段类型是 VARCHAR，不是 BOOLEAN

## 常用 SQL 模式

### 时间筛选
```sql
-- 最近 7 天
WHERE order_date >= DATE_SUB('2025-12-31', INTERVAL 7 DAY)
  AND order_date <= '2025-12-31'

-- 指定日期
WHERE order_date = '2025-12-15'

-- 月份
WHERE order_date BETWEEN '2025-12-01' AND '2025-12-31'
```

### 分组聚合
```sql
-- 按平台分组统计
SELECT platform_type,
       COUNT(DISTINCT order_no) AS order_count,
       SUM(payment_amount) AS total_sales
FROM orders
WHERE payment_amount > 0
GROUP BY platform_type
```

### 排序与限制
```sql
-- TOP 10 商品
SELECT ... ORDER BY payment_amount DESC LIMIT 10
```

## 已知陷阱

| 陷阱 | 错误示例 | 正确写法 |
|------|---------|---------|
| 错用字段 | `SELECT order_amount` | `SELECT payment_amount` |
| 退款布尔判断 | `WHERE is_refunded = true` | `WHERE is_refunded = '是'` |
| 订单数计数 | `COUNT(*)` | `COUNT(DISTINCT order_no)` |
| 时间混用 | `WHERE order_time >= '2025-12-01'` | `WHERE order_date >= '2025-12-01'` |
| 平台值写错 | `WHERE platform = 'APP'` | `WHERE platform_type = 'APP'` |

## 数据质量

- **缺失值**：`user_name` 可能有 0.5% 缺失（用 `'未知用户'` 替代）
- **重复订单**：`order_no` 应唯一，发现重复需 `DISTINCT`
- **时间戳格式**：统一 `YYYY-MM-DD HH:MM:SS`，无需 `STRFTIME` 转换
- **金额精度**：保留 2 位小数，聚合时无需 `ROUND`，显示时再 ROUND
