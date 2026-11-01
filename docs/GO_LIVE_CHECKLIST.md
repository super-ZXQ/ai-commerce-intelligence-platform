# 单客户私有部署上线检查清单

> 本清单用于试点上线验收，不代表当前已经“生产就绪”。
> 未勾选项必须在现场执行并记录证据；仅代码中存在脚本或测试不算已验收。

## A. 密钥与账号

- [ ] `.env` 权限为 `0600`，且不在 Git 中（`git check-ignore .env`）
- [ ] `JWT_SECRET` 使用 `openssl rand -hex 32` 生成，长度 ≥ 32，非示例值
- [ ] `ADMIN_PASSWORD` / `ANALYST_PASSWORD` 为 bcrypt 哈希，非明文
- [ ] `DB_ROOT_PASSWORD`、`DB_APP_PASSWORD`、`DB_AI_PASSWORD`、`DB_SYNC_PASSWORD`、
      `DB_EVENT_PASSWORD`、`DB_AUTH_PASSWORD`、`DB_MIGRATE_PASSWORD` 全部独立且随机
- [ ] 平台模型密钥 `LLM_API_KEY` 只存在于后端环境；Streamlit 无密钥
- [ ] 已轮换所有曾出现在日志/截图/聊天中的凭据

## B. 数据库与数据保留

- [ ] `db-bootstrap`、`db-migrate`、`db-grant` 均退出码 0
- [ ] `docker compose --profile verify run --rm db-verify` 全 PASS
- [ ] 空库初始化后 `orders` 行数为 102,287；`data_import_runs` 最新状态 `completed`
- [ ] 运行期创建/退款/取消各 1 笔，重启 backend 后订单与 `order_events` 仍存在
- [ ] `data_state.data_version` 随已处理事件递增，且并发事件不丢计数
- [ ] 不存在 `--include-runtime` 或启动时全量替换订单的入口

## C. 备份与恢复

- [ ] 已完成一次 `scripts/backup_mysql.sh`，记录路径与 `.sha256`
- [ ] 已完成一次 `scripts/backup_restore_drill.sh`，关键表行数一致
- [ ] 演练实测 RTO 已记录（目标 ≤ 2 小时）
- [ ] 备份保留策略与异机副本已配置，最近备份时间 < 26 小时
- [ ] 已明确 RPO = 备份间隔（≤ 24 小时），未对外承诺更低值

## D. 镜像与容器

- [ ] 生产使用 `docker-compose.prod.yml`，六个镜像变量均为不可变 tag 或 digest
- [ ] 记录全部镜像 digest 到验收记录，禁止 `latest`
- [ ] 后端/BI/AI 容器 UID 为 `10001`（`docker compose exec backend id -u`）
- [ ] 资源上限按主机规格调整（`*_CPUS` / `*_MEMORY`）
- [ ] `docker compose config --quiet`（含 prod 覆盖层）无错误

## E. TLS 与认证

- [ ] 已配置 `deploy/certs/fullchain.pem` 与 `privkey.pem`，私钥权限 `0600`
- [ ] HTTPS 访问成功，HTTP 301 跳转 HTTPS（`/nginx-health` 除外）
- [ ] `AUTH_COOKIE_SECURE=true`，Cookie 带 `HttpOnly` / `SameSite`
- [ ] 未登录访问 `/BI/`、`/ai/` 跳转 `/login`
- [ ] 登录后 BI/AI 正常，Streamlit WebSocket 握手为 101
- [ ] 账号禁用或降低权限后，下一次业务请求立即被拒绝
- [ ] `/docs`、`/metrics` 是否需要对外暴露已确认；如需限制已在 Nginx 加白名单

## F. 健康与可观测

- [ ] `/livez` 200，`/readyz` 200 且包含三个数据库检查
- [ ] 停止 MySQL 后 `/readyz` 返回 503，`/livez` 仍为 200
- [ ] Prometheus 可抓取 `/metrics`，5xx、事件写入、AI 失败有告警
- [ ] 备份任务失败、`/readyz` 连续失败有告警通道
- [ ] 日志不包含密码、Bearer、`sk-*`、`x-api-key` 明文

## G. 回滚预案

- [ ] 已记录上一版本镜像 digest
- [ ] 已保留升级前备份与校验和
- [ ] 已确认 Alembic 迁移是否支持降级；不支持时以备份恢复为准
- [ ] 回滚步骤已在预发/演练环境执行过一次（至少验证镜像切换与 `/readyz`）

## H. 验收记录

| 项 | 记录 |
|---|---|
| 验收日期（UTC） | |
| 操作人 | |
| 代码版本 / commit | |
| 镜像 digest（6 个） | |
| 备份文件与 sha256 | |
| 恢复演练结果与耗时 | |
| 权限验收输出 | |
| 未执行项与原因 | |

## 未执行项（截至 2026-11-01，本仓库开发环境）

- [ ] Docker 冷启动全栈（本机 Docker daemon 不可用）
- [ ] 真实最小权限账号验收（CI 已配置 `db-verify`，尚未在目标环境执行）
- [ ] 备份/恢复演练（脚本与 CI 步骤已就绪，本机未执行）
- [ ] TLS 握手与证书链验证（无证书与运行中的 Nginx）
- [ ] 多 worker / 高可用 / binlog PITR（当前不在实施范围）
