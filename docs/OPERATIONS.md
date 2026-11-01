# 运维手册：SLO、备份恢复、探针与故障处置

本文面向单客户私有部署。所有数字分为两类：**已验证**（有测试或实测证据）与
**目标值**（尚未经过生产周期验证）。未标注“已验证”的指标不得对外宣称已达成。

## 1. 部署边界

- 单节点 Docker Compose，Nginx 统一入口；不包含多副本、跨可用区或 Kubernetes。
- 后端单 uvicorn worker：AI 并发、每日额度与主体限流计数在进程内准确；多 worker
  会按 worker 放大，不作为当前支持范围。
- MySQL 单实例；Redis 单实例（`allkeys-lru`），会话逻辑 DB 只做命名隔离，
  不解决淘汰隔离。
- 不在本文档范围内：多租户、在线支付、计费、PITR、自动故障转移。

## 2. 服务级别目标（内部目标，非合同 SLA）

| 指标 | 目标 | 测量来源 | 当前状态 |
|---|---|---|---|
| 统一入口可用性 | 99.5%/月（计划维护除外） | 外部拨测 `/nginx-health` | 目标，未积累 30 天基线 |
| 后端读接口 P95 | 缓存命中 < 500ms；数据库查询 < 2s | `/metrics`、访问日志 | 目标，仅本地/CI 冒烟 |
| AI 查询成功率 | ≥ 95%（不含额度/并发拒绝与用户取消） | `ai_query_audit` + `/metrics` | 目标，真实模型评测为离线样本 |
| 订单事件写入成功率 | ≥ 99.9%（不含客户端 4xx） | 事件指标 + 应用日志 | 目标，CI 覆盖幂等/乱序/重启保留 |
| 备份可恢复性 | 每次演练关键表行数一致 | `scripts/backup_restore_drill.sh` | 脚本与 CI 步骤已就绪，本机未执行 |

告警建议：`/readyz` 连续 3 次失败、5xx 比例 > 1%（5 分钟）、事件写入连续
`EVENT_WRITE_*`、磁盘使用率 > 80%、备份任务失败或超过 26 小时无新备份。

## 3. RPO、RTO 与可接受数据损失

| 项 | 当前承诺 | 说明 |
|---|---|---|
| RPO（恢复点目标） | **≤ 24 小时** | 依赖每日逻辑备份；未配置 binlog，不能承诺更低 RPO |
| 可接受数据损失 | 最近一次成功备份之后的订单事件与认证变更 | 订单 CSV 只是初始化快照，不是运行期账本 |
| RTO（恢复时间目标） | **≤ 2 小时（目标，未实测）** | 以恢复演练实测值为准，演练后回填 |
| Redis 数据 | 可全部丢失 | 会话丢失 = 用户重新登录；不影响订单/事件/认证数据 |
| 备份保留 | 建议 ≥ 14 天，并保留一份异机副本 | 通过 `--prune-days 14` 显式启用清理 |

若业务要求 RPO < 24 小时，必须增加 MySQL binlog 与定期全量备份的 PITR 方案；
在完成该方案与演练前，不得对外承诺更低 RPO。

## 4. 备份与恢复

### 4.1 每日备份

```bash
# 手动执行；stdout 输出备份文件路径，日志在 stderr
scripts/backup_mysql.sh

# 建议的 cron（03:00，保留 14 天）
0 3 * * * cd /opt/ai-commerce-intelligence-platform && scripts/backup_mysql.sh --prune-days 14 >> /var/log/ea-backup.log 2>&1
```

备份特征：

- 使用 `mysqldump --single-transaction`，InnoDB 表在同一快照内导出，不锁业务表。
- 密码只在 mysql 容器内通过 `MYSQL_ROOT_PASSWORD` 展开，不进入宿主机命令行。
- 生成 `<file>.sql.gz` 与 `<file>.sha256`；gzip 损坏或缺少 `Dump completed` 会失败退出。
- 备份目录默认 `backups/`（已 gitignore），权限 `0700/0600`。

### 4.2 恢复到新库（推荐）

```bash
scripts/restore_mysql.sh backups/<db>-20261101T030000Z.sql.gz
# 默认恢复到 <db>_restore_drill，目标库非空会拒绝
```

恢复脚本默认拒绝覆盖生产库；只有显式 `--allow-overwrite` 且目标库为空（或已确认）
才继续。恢复到生产库前必须停止事件写入并先做一次独立备份。

### 4.3 恢复演练（验收证据）

```bash
scripts/backup_restore_drill.sh
# --keep 保留临时库 drill_<时间戳> 与备份文件，便于人工核查
```

演练流程：备份 → 恢复 `drill_*` 临时库 → 对比 `orders`、`order_events`、
`auth_users`、`data_state`、`alembic_version` 行数。只有演练通过，才算“备份可用”。
CI 的 Docker smoke 已加入该步骤；本机未执行时必须在验收记录中标注“未执行”。

## 5. 健康检查与探针

| 端点 | 语义 | 依赖 | 用途 |
|---|---|---|---|
| `/livez` | 进程存活 | 无 | 容器重启判断；数据库故障时不触发重启 |
| `/readyz` | 可接流量 | 业务库、事件库、认证库 | Compose healthcheck、负载均衡摘流；任一失败返回 503 |
| `/health` | 兼容旧监控 | 业务库 | 保留原语义，返回 healthy/degraded |
| `/health/detailed` | 组件明细 | 业务库/缓存/Redis | 人工排障；公开只读 |

`/readyz` 只返回布尔检查结果，不回显驱动异常或连接串。Dockerfile 与
`docker-compose.yml` 均使用 `/readyz` 作为健康检查；`/livez` 用于区分
“进程死了”与“依赖不可用”。

## 6. 503 分类与处置

| 错误码/端点 | 触发条件 | 处理 |
|---|---|---|
| `EVENT_WRITE_TIMEOUT` | 单事件处理超过 `ORDER_EVENT_TIMEOUT_SECONDS`（默认 5s） | MySQL 锁等待/慢查询；用同一 `event_id` 幂等重试 |
| `EVENT_WRITE_UNAVAILABLE` | 事件事务异常（连接、权限、约束） | 查看后端日志；确认 `ea_events` 授权与 MySQL 状态后重试 |
| `AI_QUEUE_TIMEOUT` | 全局 AI 并发已满且排队超过 `AI_QUEUE_TIMEOUT_SECONDS` | 属于容量保护；稍后重试或提高并发（需评估成本） |
| `RFM_DATA_UNAVAILABLE` | 订单表为空或不可用 | 先完成数据导入；不要把它当 500 重试风暴 |
| `/api/monitor/rag-stats` 503 | RAG 统计文件尚未生成 | AI 助手未启动或未发生检索；不影响 data 分支 |
| `/readyz` 503 | 任一数据库连接池不可用 | 检查 MySQL/账号/网络；此时不应接收新流量 |

历史 503 根因（2026-09-18 Docker 审计）：`orders.order_seq_id` 缺少
`AUTO_INCREMENT` 且事件路径未显式分配主键，导致 `ORDER_CREATED` 返回
`EVENT_WRITE_UNAVAILABLE`；已由 Alembic `0002_order_auto_increment` 修复，
回归测试见 `backend/tests/test_order_event_create_pk.py`。详见
`docs/RESILIENCE.md`。

## 7. 升级与回滚

升级前必须完成：

1. `scripts/backup_mysql.sh` 成功，并记录备份路径与校验和。
2. 记录当前各服务镜像 digest（`docker compose images` 或 `docker inspect`）。
3. 在维护窗口停止事件写入（或确认无写入方）。

升级：

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
curl --fail http://localhost/readyz
```

`db-migrate` 是一次性 Alembic 升级容器，`db-grant` 在其成功后收紧授权。
回滚优先使用**上一版本镜像 digest + 升级前备份恢复**；Alembic 降级只在迁移
明确支持且已演练时使用，不能替代备份。

## 8. TLS 与安全配置

- 默认 Compose 仅 HTTP，适合内网试点；生产对外必须启用 HTTPS。
- 使用 `docker-compose.tls.yml` 叠加：Nginx 在 443 终止 TLS，80 仅保留
  `/nginx-health` 并 301 跳转；强制 `AUTH_COOKIE_SECURE=true`。
- 证书放在 `deploy/certs/`（已 gitignore）：`fullchain.pem`、`privkey.pem`。
- TLS 配置保留 `/ai/`、`/BI/` 的 `auth_request` 与 WebSocket `Upgrade` 头；
  `backend/tests/test_operations.py` 会校验关键块存在。
- 若 TLS 由外部负载均衡终止，不要叠加 TLS 层，改为自行设置
  `AUTH_COOKIE_SECURE=true` 与 `AUTH_COOKIE_SAMESITE=lax`。

## 9. 容器加固与资源上限

- 应用镜像以 UID/GID `10001` 非 root 运行；`no-new-privileges:true`。
- Compose 为 MySQL/Redis/后端/BI/AI/Nginx 设置 CPU 与内存上限，见
  `deploy/.env.example` 中的 `*_CPUS` / `*_MEMORY`。
- 生产使用 `docker-compose.prod.yml`，所有镜像（含 MySQL/Redis/Nginx）必须
  固定 tag 或 digest；缺少变量会直接拒绝启动，禁止 `latest`。
- 数据库账号矩阵与验收命令见 `docs/DB_PERMISSIONS.md`：
  `docker compose --profile verify run --rm db-verify`。

## 10. 已知限制与未执行项

- 本机 Docker daemon 不可用：**容器冷启动、重启保留、真实授权验收、TLS 握手、
  备份恢复演练均未在本机执行**；相关脚本与 CI 步骤已就绪。
- 未配置 binlog/PITR，RPO 只能承诺 ≤ 24 小时。
- 单机单实例，无自动故障转移；MySQL/Redis 故障期间服务不可用或降级。
- 限流、AI 并发与每日额度为进程内计数，多 worker 部署会放大配额。
- `docs/LOAD_TEST_REPORT.md` 的负载数据来自既有环境，不能替代本次验收。
