# 负载测试报告

状态：**已执行部分场景；未执行项明确标注。本文件只记录真实命令输出，不含估算性能宣传。**

## 环境（2026-09-18 审计）

| 项 | 值 |
|---|---|
| Compose 项目 | `ea-audit-20260918` |
| 入口 | `http://127.0.0.1:18080`（Nginx） |
| 数据库 | MySQL 8，库名 `ai_commerce_event_docker_audit` |
| 镜像 | `ea-audit-20260918-backend` / `-streamlit` / `-ai-assistant` |
| 认证 | 审计 JWT（admin），不落盘到结果摘要 |
| 合成生产者 | `scripts/order_event_producer.py` |
| 限流配置 | `/api/order-events` = **120 请求 / 60 秒 / 客户端**（`backend/utils/rate_limiter.py`） |

## 已执行：事件摄入负载（order_event_producer）

目标 URL：`http://127.0.0.1:18080/api/order-events`。发送端按计划 RPS 出队；服务端限流会导致大量 HTTP 429，**429 计入 errors，不伪装成成功**。

### 10 RPS · 60 秒 · seed=20260918

命令：

```powershell
python scripts/order_event_producer.py --url http://127.0.0.1:18080/api/order-events `
  --token <JWT> --rps 10 --duration 60 --concurrency 10 --seed 20260918 `
  --output output/audit_load/rps10_seed20260918.json
```

真实结果（摘录）：

| 指标 | 值 |
|---|---|
| attempted | 709 |
| processed | 201 |
| duplicates | 11 |
| rejected | 2 |
| http_429 | 488 |
| http_503 | 7 |
| errors 合计 | 495 |
| elapsed_seconds | 70.831 |
| 吞吐（发送侧） | 10.01 RPS |
| P50 / P95 / P99 (ms) | 3.638 / 29.459 / 78.485 |
| API 错误率（相对 attempted） | 495/709 ≈ **69.8%**（主体为限流 429） |

解读：限流窗口内约 120 次/分钟可写入，其余被拒绝；**不能**把本档结果表述为“系统稳定承载 10 RPS 无错误”。

### 50 RPS · 20 秒 · seed=20260919

| 指标 | 值 |
|---|---|
| attempted | 1144 |
| processed | 2 |
| rejected | 2 |
| http_429 | 1125 |
| http_503 | 15 |
| P50 / P95 / P99 (ms) | 3.293 / 6.617 / 106.922 |
| throughput_rps（发送侧） | 50.032 |

### 100 RPS · 15 秒 · seed=20260920

| 指标 | 值 |
|---|---|
| attempted | 1720 |
| processed | 83 |
| rejected | 18 |
| http_429 | 1619 |
| http_503 | 0 |
| P50 / P95 / P99 (ms) | 4.065 / 23.898 / 36.233 |
| throughput_rps（发送侧） | 100.061 |

说明：50/100 RPS 档**已真实执行**，但有效业务写入仍受 120/60s 客户端限流约束；延迟分位仅覆盖**成功到达处理路径的请求**（含 200/422），429 未计入延迟样本的语义需按生产者实现理解（本脚本对 429 不记录 latency）。

## 负载后数据一致性（MySQL 实收）

| 检查 | 结果 |
|---|---|
| CSV 基线订单（非 SYN/AUDIT） | **102,287** |
| SYN 订单行 | **269** = ACTIVE 252 + REFUNDED 17 |
| 每个 SYN 订单均有 order_events | **269/269** |
| SYN 账本 PROCESSED | **286**（= 252 创建 + 17×2 创建&退款，与状态机一致） |
| SYN 账本 REJECTED | 21（乱序/非法路径留痕） |
| 总 orders | 102,558（基线 + 审计/合成） |
| Prometheus `db_timeouts_total` | **0** |
| Prometheus `order_events_processed_total`（审计栈累计） | processed 289 / rejected 23 / duplicate 17 / error 22 |

**一致性结论**：限流与少量 503 未造成 orders 与账本半提交；ACTIVE+REFUNDED 订单数与 SYN 订单总数一致，且全部有账本。

## 已执行：Redis 停机实验

| 阶段 | 证据 |
|---|---|
| baseline | `/health/detailed` = healthy，cache.backend=`redis` |
| `docker stop ea-redis` | 业务 API（sales-overview / sales-trend / rfm）仍 **HTTP 200**；`/health/detailed` = **degraded**；`redis.status=error`（Timeout connecting）；`cache.backend=redis_fallback_memory`；`/health` 与 `/metrics` 仍 200 |
| `docker start ea-redis` | `/health/detailed` 恢复 healthy，`cache.backend=redis`，keys=6 |

证据文件：`output/audit_redis_degrade_evidence.json`。

## 未执行（明确标注）

| 场景 | 状态 | 原因 |
|---|---|---|
| Locust 混合读写 `load/locustfile.py` | **未执行** | 当前 `.venv` 无 `locust`（`ModuleNotFoundError: locust`）；**未**修改 requirements 安装依赖 |
| 并发读取 20/50 用户专项 | **未执行** | 本轮验收聚焦事件摄入 + Redis 降级 |
| MySQL 延迟 / 连接池耗尽注入 | **未执行** | 未做故障注入 |
| LLM 429 mock / 真实模型负载 | **未执行** | 审计环境无真实 LLM Key，且禁止伪造 |
| 多客户端绕过单 IP 限流后的服务容量 | **未执行** | 需多源 IP 或调限流策略，超出本轮范围 |

## 关键结论（基于实测）

1. **单客户端写入容量受 API 限流（120/min）约束**，不是无限吞吐；高 RPS 下错误率主要来自 429。
2. 在限流允许的写入量内，**幂等、乱序拒绝、退款状态机与账本一致性成立**。
3. Redis 停机时读路径可用，健康检查明确 **degraded**，恢复后自动回到 redis backend。
4. 并发写入下出现少量 **HTTP 503**（10 RPS 档 7 次、50 RPS 档 15 次）；`db_timeouts_total=0`，数据无半提交。503 根因需单独压测/池监控才能定性，本轮不升级为“已知缺陷已修复”。
