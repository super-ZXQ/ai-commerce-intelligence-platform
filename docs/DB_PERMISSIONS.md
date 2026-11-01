# 数据库权限矩阵

本文件描述私有部署下各数据库账号的权限边界。权威实现是
`deploy/mysql/bootstrap-users.sh`（创建账号）与 `deploy/mysql/apply-grants.sh`
（Alembic 迁移完成后收紧为表级授权），测试
`backend/tests/test_alembic_migrations.py` 会校验两者与本文档一致。

## 账号与连接映射

| 账号 | 使用方 | 配置项 | 权限级别 |
|------|--------|--------|----------|
| `ea_app` | FastAPI 业务读路径、BI 查询 | `DB_USER` / `DB_PASSWORD` | 表级只读 |
| `ea_ai` | AI Text-to-SQL 只读执行 | `AI_DB_USER` / `AI_DB_PASSWORD` | 仅 `orders` 表级只读 |
| `ea_events` | 订单事件摄入写路径 | `EVENT_DB_USER` / `EVENT_DB_PASSWORD` | `orders` / `order_events` / `data_state` 表级读写 |
| `ea_auth` | 登录、权限、服务凭据、审计 | `AUTH_DB_USER` / `AUTH_DB_PASSWORD` | 仅认证/审计表 |
| `ea_sync` | 显式订单导入、staging 换表 | `MIGRATION_DB_USER=ea_sync`（data-import） | 库级 DDL（运维例外） |
| `ea_migrate` | Alembic schema 升级 | `MIGRATION_DB_USER=ea_migrate` | 库级 DDL（迁移例外） |
| `root` | 仅 bootstrap / apply-grants | MySQL 容器内 | 全部 |

## 表级权限

| 表 | `ea_app` | `ea_ai` | `ea_events` | `ea_auth` |
|----|----------|---------|-------------|-----------|
| `orders` | SELECT | SELECT | SELECT/INSERT/UPDATE | — |
| `order_events` | SELECT | — | SELECT/INSERT/UPDATE | — |
| `data_state` | SELECT | — | SELECT/UPDATE | — |
| `data_import_runs` | SELECT | — | — | — |
| `order_status_reconciliation` | SELECT | — | — | — |
| `auth_users` | — | — | — | SELECT/INSERT/UPDATE/DELETE |
| `service_clients` | — | — | — | SELECT/INSERT/UPDATE/DELETE |
| `auth_audit_log` | — | — | — | SELECT/INSERT |
| `ai_query_audit` | — | — | — | SELECT/INSERT |
| `app_metadata` | — | — | — | — |

说明：

- `ea_ai` 只授予 `orders`，即使 Text-to-SQL 黑名单被绕过，也无法读取认证表或其他业务表。
- `ea_events` 不能删除订单、不能访问认证表；事件幂等由 `order_events.event_id` 唯一键兜底。
- `ea_events` 可在事件事务内更新 `data_state.data_version`，用于缓存版本切换；该权限不包含 DELETE，不能重置版本或删除版本行。
- `ea_auth` 无任何 `orders` 权限；认证库连接池与业务连接池相互独立。
- `ea_app` 不授予 `auth_users` / `service_clients`，避免业务接口意外泄露口令哈希。
- 审计表只允许追加与查询，不允许认证账号修改历史审计。

## 运维例外：`ea_sync` 与 `ea_migrate`

`ea_sync` 需要 `CREATE/DROP/ALTER/INDEX` 在 staging 表上完成校验后原子换表，
并在失败时保留备份表；`ea_migrate` 需要 DDL 执行 Alembic。两者都是**显式运维动作**，
不注入 API、AI 助手或持续运行的服务容器：

- `ea_sync` 不授予 `FILE`，导入使用应用侧批量 INSERT，不依赖 `LOAD DATA`。
- `ea_migrate` 只在 `db-migrate` 一次性容器中使用。
- 两者均保留库级权限，无法按表拆分为纯 DML 账号；这是当前数据迁移方式的已知例外。
- 运行期禁止用 `ea_sync` 全量替换已有订单；导入脚本会拒绝覆盖运行期数据。

## 验收方法

1. 一键验收（真实账号，只做只读与 0 行写入探测，不改业务数据）：

```bash
docker compose --profile verify run --rm --no-deps db-verify
```

脚本 `deploy/mysql/verify-grants.sh` 会逐项输出 PASS/FAIL；CI 的 Docker smoke
也执行同一步骤。没有 PASS 输出不能视为授权已生效。

2. 使用各账号分别连接，执行允许与禁止的语句：

```sql
-- ea_ai：应成功
SELECT COUNT(*) FROM orders;
-- ea_ai：应失败（1142）
SELECT COUNT(*) FROM auth_users;

-- ea_auth：应成功
SELECT COUNT(*) FROM auth_users;
-- ea_auth：应失败（1142）
SELECT COUNT(*) FROM orders;

-- ea_events：应成功
INSERT INTO order_events (...) VALUES (...);
UPDATE data_state SET data_version = data_version + 1 WHERE id = 1;
-- ea_events：应失败（1142）
SELECT COUNT(*) FROM auth_users;
DELETE FROM data_state WHERE id = 1;
```

3. 迁移后重新执行 `docker compose run --rm db-grant`，确认脚本幂等且不产生新授权错误。
4. 真实 MySQL 上的账号验收属于部署验收项；仅阅读脚本或单元测试不足以证明权限已生效。
   验收记录写入 `docs/GO_LIVE_CHECKLIST.md`。
