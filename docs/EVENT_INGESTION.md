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

统一指标口径：只有 `CANCELLED` 订单不计入经营指标。销售额/GMV、趋势、商品排行、品类分析和 RFM 均以“非取消订单”为基数；退款订单保留在销售额与订单数中（毛额口径），退款金额与退款率单独累计。退款率 = 退款订单数 ÷ 非取消订单数，按订单日期归因（订单队列口径）；事件账本中的退款发生时间不参与 BI/AI 指标，避免历史 CSV 与运行期数据口径分裂。

处理成功时在**同一事务内**执行 `UPDATE data_state SET data_version = data_version + 1 WHERE id = 1`，提交后仅失效 `analytics` 和 `rfm` 标签缓存，并清除 RFM 进程内快照；不执行 Redis `FLUSHDB`。版本更新与订单/账本同提交、同回滚，因此不会出现“数据已变但版本未变”或“版本已变但数据回滚”的错位。

缓存 key 的版本语义：

- `analytics` / `rfm` 的缓存装饰器在查询前从**同一数据库会话**读取 `data_state.data_version`，并把 `v{version}` 拼入 key。读取发生在业务查询之前，MySQL REPEATABLE READ 下版本与后续查询共享同一快照。
- 事件提交后新请求读取到新版本，自然落到新 key，不依赖标签失效是否成功；旧 key 随 TTL 过期，不会被再次读取。
- 版本读取失败（旧库未迁移、连接异常）时回退到纯 TTL 语义，此时允许的陈旧窗口等于该接口 TTL（分析类 120/180 秒、RFM 300/600 秒）。标签失效成功时陈旧窗口为 0；多进程环境下每个进程的 RFM 进程内快照依赖版本 key 切换。
- `UPDATE data_state` 会串行化并发事件事务（单行锁）。事件摄入是低频管理操作，单行锁只影响写路径，不阻塞读查询；隔离 MySQL 并发测试验证 N 笔并发事件全部成功且版本恰好 +N。
- `/api/bi/orders` 在每次请求开始时先读 `data_version`（同时建立本事务快照），响应返回该版本；客户端首屏之后的页必须携带 `data_version`，服务端发现不一致返回 409，客户端整次重读（最多 3 次）。服务端在查询前后各读一次版本，READ COMMITTED 下也能兜住查询窗口内已提交的写入。

`GET /api/order-events/freshness` 返回：

- `data_updated_at`
- `last_event_occurred_at`
- `cache_generated_at`
- `freshness_seconds`

## 已有数据卷迁移

Docker 空卷由 `db-migrate` 执行 `alembic upgrade head`，不会再运行 MySQL `initdb.d`。已有数据卷不会自动迁移，先备份后手动执行：

```bash
python -m alembic -c backend/alembic.ini upgrade head
```

`0001_baseline` 创建 `order_events` 并为存量 `orders` 补齐 `order_status`、`event_version`、`updated_at`；已有订单保留并初始化为 `ACTIVE/version=0`，不会重导或删除 CSV 数据。
`0002_order_auto_increment` 为存量订单主键补齐自增；`0003_data_protection` 增加 `data_state`、`data_import_runs`、`order_status_reconciliation` 审计表。
需要导入公开快照时使用 `python backend/scripts/import_orders.py --mode bootstrap`；已有运行期订单或事件账本时禁止全量替换。

## 可复现实验流量

[`scripts/order_event_producer.py`](../scripts/order_event_producer.py) 只从公开样本提取商品/平台枚举，生成新的合成订单号和用户标识；默认只访问 `localhost`，不会发送真实个人信息。固定 `--seed` 可复现同一事件序列。

```powershell
.venv\Scripts\python.exe scripts/order_event_producer.py --seed 20260918 --duration 60 --rps 10
```

支持通过 `--duplicate-rate`、`--out-of-order-rate`、`--invalid-rate` 和 `--refund-rate` 注入可控异常。输出只报告发送/成功/重复/拒绝数及延迟分位数；它不是线上真实订单流量。
