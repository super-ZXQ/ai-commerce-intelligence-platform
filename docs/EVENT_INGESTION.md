# 订单事件摄入

## 范围与数据来源

`data/cleaned_orders.csv` 是 **102,287 条公开订单的 bootstrap 快照**，仅用于首次初始化。
运行后的新增订单、退款和取消必须通过 `POST /api/order-events` 写入；本项目不声称接入了真实电商生产事件流。

支持的事件：`ORDER_CREATED`、`ORDER_REFUNDED`、`ORDER_CANCELLED`。

## 写入边界

| 账号 | 权限 | 用途 |
|---|---|---|
| `ea_app` | SELECT | 常规 API 查询 |
| `ea_ai` | SELECT | Agent / Text-to-SQL，只读 |
| `ea_events` | orders/order_events 的 SELECT、INSERT、UPDATE | 事件摄入 |
| `ea_sync` | 导入与原子换表所需权限 | CSV bootstrap/同步 |

Agent 不读取事件写账号的连接串，LLM 也不会获得任何写库工具。

## API

单条事件可直接提交；小批量使用 `{ "events": [...] }`，上限默认为 100 条。

```json
{
  "event_id": "5b5dde53-dcfc-4719-a553-6a36ac1a93c3",
  "order_id": "SYN-00000001-00000000",
  "event_type": "ORDER_CREATED",
  "occurred_at": "2026-01-01T00:00:00Z",
  "version": 1,
  "source": "synthetic-producer",
  "payload": {
    "user_name": "synthetic_user_0000",
    "product_id": "PRODUCT-001",
    "order_amount": "100.00",
    "payment_amount": "95.00",
    "discount_amount": "5.00",
    "platform_type": "APP"
  }
}
```

`ORDER_REFUNDED` 与 `ORDER_CANCELLED` 的 payload 只允许可选的 `reason`，不允许借事件修改金额、用户或商品字段。

## 幂等与乱序策略

`order_events.event_id` 有数据库唯一约束。相同 `event_id` 的重复请求返回 `duplicate`，不会再次修改订单或 GMV。

每个 `order_id` 维护 `event_version` 水位：

- 新事件的 version 必须严格大于水位；低版本/同版本且不同 event_id 以 `rejected` 留在账本；
- `ORDER_CREATED` 只能创建不存在的订单；
- 退款或取消只能从 `ACTIVE` 转换，退款后不能再取消，取消后不能再退款；
- 状态不安全、订单不存在等情况也记录为 `REJECTED` 和脱敏失败原因，不静默丢弃。

账本插入与 `orders` 读模型更新在同一 MySQL 事务中完成：成功时两者同时提交，数据库错误时两者同时回滚。

## 指标口径与缓存

事件读模型中 `ACTIVE` 订单计入销售额/GMV、趋势、商品排行和 RFM；退款与取消会从这些有效经营指标中移除。退款金额仍由退款状态独立统计。

处理成功后仅失效 `analytics` 和 `rfm` 标签缓存，并清除 RFM 进程内快照；不执行 Redis `FLUSHDB`。`GET /api/order-events/freshness` 返回：

- `data_updated_at`
- `last_event_occurred_at`
- `cache_generated_at`
- `freshness_seconds`

## 已有数据卷迁移

Docker 空卷会执行 `sql/01_create_table.sql`。已有 MySQL 数据卷不会重新执行 initdb，先备份后手动运行：

```powershell
.venv\Scripts\python.exe backend/scripts/migrate_event_schema.py
```

该脚本只创建 `order_events`，并为 `orders` 补齐 `order_status`、`event_version`、`updated_at`；已有订单保留并初始化为 `ACTIVE/version=0`，不会重导或删除 CSV 数据。

## 可复现实验流量

[`scripts/order_event_producer.py`](../scripts/order_event_producer.py) 只从公开样本提取商品/平台枚举，生成新的合成订单号和用户标识；默认只访问 `localhost`，不会发送真实个人信息。固定 `--seed` 可复现同一事件序列。

```powershell
.venv\Scripts\python.exe scripts/order_event_producer.py --seed 20260918 --duration 60 --rps 10
```

支持通过 `--duplicate-rate`、`--out-of-order-rate`、`--invalid-rate` 和 `--refund-rate` 注入可控异常。输出只报告发送/成功/重复/拒绝数及延迟分位数；它不是线上真实订单流量。
