#!/bin/sh
set -eu

# 表级最小权限：必须在 db-migrate 成功后执行，因此可以安全引用具体表名。
# 该脚本幂等：重复执行会先撤销库级权限再重新授予表级权限。
#
# 权限矩阵见 docs/DB_PERMISSIONS.md：
#   ea_app    业务只读（orders/order_events/data_state/导入审计）
#   ea_ai     AI Text-to-SQL 只读，仅 orders
#   ea_events 订单与事件写入
#   ea_auth   认证、服务凭据与审计表
#   ea_sync   离线导入（保留库级 DDL，见文档例外说明）
#   ea_migrate Alembic 迁移（保留库级 DDL）

case "${DB_NAME}" in
  *[!A-Za-z0-9_]*|'')
    echo "DB_NAME 只能包含字母、数字和下划线" >&2
    exit 1
    ;;
esac

if [ -z "${DB_AUTH_PASSWORD:-}" ]; then
  echo "DB_AUTH_PASSWORD 未设置" >&2
  exit 1
fi

escape_sql_string() {
  printf '%s' "$1" | sed "s/\\\\/\\\\\\\\/g; s/'/''/g"
}

AUTH_PASSWORD_ESCAPED="$(escape_sql_string "${DB_AUTH_PASSWORD}")"

attempt=1
until mysql --protocol=TCP -h mysql -uroot -e "SELECT 1" >/dev/null 2>&1; do
  if [ "${attempt}" -ge 30 ]; then
    echo "等待 MySQL 连接超时" >&2
    exit 1
  fi
  echo "等待 MySQL 接受连接（${attempt}/30）..."
  sleep 2
  attempt=$((attempt + 1))
done

# 迁移完成后再校验表是否存在，避免在空库上授予不存在的表。
missing="$(mysql --protocol=TCP -h mysql -uroot -N -B -e "
  SELECT COUNT(*) FROM information_schema.tables
  WHERE table_schema='${DB_NAME}'
    AND table_name IN ('orders','order_events','data_state','data_import_runs',
                       'order_status_reconciliation','auth_users','service_clients',
                       'auth_audit_log','ai_query_audit')
")"
if [ "${missing}" != "9" ]; then
  echo "schema 尚未就绪：期望 9 张表，实际 ${missing} 张；请先完成 db-migrate" >&2
  exit 1
fi

for user in ea_app ea_ai ea_events ea_auth; do
  # 旧版本授予的是库级权限；撤销失败（本就没有库级授权）不视为错误。
  mysql --protocol=TCP -h mysql -uroot \
    -e "REVOKE ALL PRIVILEGES ON \`${DB_NAME}\`.* FROM '${user}'@'%';" \
    >/dev/null 2>&1 || true
done

mysql --protocol=TCP -h mysql -uroot <<SQL
CREATE USER IF NOT EXISTS 'ea_auth'@'%' IDENTIFIED BY '${AUTH_PASSWORD_ESCAPED}';
ALTER USER 'ea_auth'@'%' IDENTIFIED BY '${AUTH_PASSWORD_ESCAPED}';

-- 业务只读账号：可以读订单、事件、数据版本与导入审计，不能读认证/审计表。
GRANT SELECT ON \`${DB_NAME}\`.\`orders\` TO 'ea_app'@'%';
GRANT SELECT ON \`${DB_NAME}\`.\`order_events\` TO 'ea_app'@'%';
GRANT SELECT ON \`${DB_NAME}\`.\`data_state\` TO 'ea_app'@'%';
GRANT SELECT ON \`${DB_NAME}\`.\`data_import_runs\` TO 'ea_app'@'%';
GRANT SELECT ON \`${DB_NAME}\`.\`order_status_reconciliation\` TO 'ea_app'@'%';

-- AI 只读账号：Text-to-SQL 只允许访问 orders，缩小越权查询的影响面。
GRANT SELECT ON \`${DB_NAME}\`.\`orders\` TO 'ea_ai'@'%';

-- 事件写账号：只写订单读模型、事件账本与数据版本，无审计/认证表权限。
GRANT SELECT, INSERT, UPDATE ON \`${DB_NAME}\`.\`orders\` TO 'ea_events'@'%';
GRANT SELECT, INSERT, UPDATE ON \`${DB_NAME}\`.\`order_events\` TO 'ea_events'@'%';
-- 版本行在事件事务内自增；UPDATE 读取 data_version 需要列级 SELECT。
GRANT SELECT, UPDATE ON \`${DB_NAME}\`.\`data_state\` TO 'ea_events'@'%';

-- 认证账号：仅认证、服务凭据与审计表；无业务数据访问权限。
GRANT SELECT, INSERT, UPDATE, DELETE ON \`${DB_NAME}\`.\`auth_users\` TO 'ea_auth'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON \`${DB_NAME}\`.\`service_clients\` TO 'ea_auth'@'%';
GRANT SELECT, INSERT ON \`${DB_NAME}\`.\`auth_audit_log\` TO 'ea_auth'@'%';
GRANT SELECT, INSERT ON \`${DB_NAME}\`.\`ai_query_audit\` TO 'ea_auth'@'%';

FLUSH PRIVILEGES;
SQL

echo "表级最小权限已应用"
