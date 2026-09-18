# 韧性与降级边界

## 已实现的行为

| 故障 | 行为 | 证据 |
|---|---|---|
| Redis 不可用 | 缓存/会话降级到有界内存，health 标为 `degraded` | `backend/utils/cache.py` + 本轮 Docker 实验 |
| 缓存同时过期 | 每 key 单飞锁，只有一个重建者 | `@cached` 与 `test_core_safety.py` |
| MySQL 慢 SQL | Agent 注入 `MAX_EXECUTION_TIME`，协程 timeout 仅兜底 | `agent_core/sql_safety.py` |
| 事件写入超时 | 5 秒上限，返回稳定 503，不无限重试 | `routes/order_events.py` |
| LLM 429/超时/结构错误 | 最多一次 SQL 纠错，失败后给出可控错误 | `agent_core/runtime.py` |
| RAG 不可用 | knowledge/hybrid 无来源降级；data 分支不依赖 RAG | `agent_core/runtime.py` |

所有公开 trace 只记录节点摘要和错误类别，不记录 API Key、原始提示词或隐藏思维链。

## 验证矩阵（2026-09-18 Docker 审计更新）

| 场景 | 验证点 | 当前状态 |
|---|---|---|
| 相同 event_id 并发/重复提交 | 一条 `processed`、其余 `duplicate` | **Docker 实收通过**（`AUDIT-SM-*`：create processed → 同 id duplicate） |
| 乱序事件 | 低版本记录 `rejected`，状态不倒退 | **Docker 实收通过**（reason=`事件版本不高于当前订单版本`） |
| 终态切换 | REFUNDED 后不可改 CANCELLED | **Docker 实收通过**（reason=`订单当前为 REFUNDED，不能转换为 CANCELLED`） |
| 事务失败 | 账本与 orders 不出现半提交 | 隔离 MySQL 集成测试已通过（2026-09-18）；Docker 负载后 SYN 订单与账本一致 |
| Redis 停止 | 业务读取继续、health=`degraded` | **Docker 实收通过**（见 `docs/LOAD_TEST_REPORT.md` 与 `output/audit_redis_degrade_evidence.json`） |
| MySQL 延迟/池耗尽 | 超时且无无限重试 | CI 慢查询测试已有；**混合负载/延迟注入未执行** |
| LLM 429 / 非法 JSON | 有限重试与明确错误 | Runtime 单元测试已有；**真实网络实验未执行**（无 API Key） |
| RAG/Chroma 停止 | data 仍可查询、knowledge 明确降级 | Runtime 单元测试已有；**容器实验未执行** |
| 订单事件限流 | 单客户端写入受 `120/60s` 约束 | **Docker 实收**：10/50/100 RPS 发送时大量 429，见 LOAD_TEST_REPORT |

## 本轮修复的可复现缺陷（Docker 冷启动）

| 缺陷 | 现象 | 修复 |
|---|---|---|
| `orders.order_seq_id` 无 AUTO_INCREMENT，且事件路径未显式分配 PK | `ORDER_CREATED` API 返回 503 `EVENT_WRITE_UNAVAILABLE` | `sql/01_create_table.sql` 增加 AUTO_INCREMENT；`order_event_service` 创建时 `max(id)+1` 显式赋值；`migrate_event_schema.py` 幂等 ALTER |
| `sales-trend` 对 `order_time IS NULL` 的 `date_format` 结果 | API 500 `SalesTrendItem.period` 校验失败 | `analytics_service.get_sales_trend` 跳过 `period is None` 行 |

回归测试：`backend/tests/test_order_event_create_pk.py`（3 项，本地 pytest 通过）。

## 故障注入命令备忘

```powershell
docker stop ea-redis
# 观察 /health/detailed 与业务 API
docker start ea-redis
```

更完整负载命令与真实分位结果见 `docs/LOAD_TEST_REPORT.md`。未执行项不得用估算值替代。
