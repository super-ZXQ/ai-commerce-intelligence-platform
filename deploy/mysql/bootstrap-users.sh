#!/bin/sh
set -eu

# 该脚本由一次性 db-bootstrap 容器执行，适用于空卷与已有数据卷。
# 注意：Schema 由 db-migrate（Alembic）在 bootstrap 之后创建，因此这里
# 不能对 orders/order_events 做表级 GRANT——空库上表尚不存在，MySQL 会报 1146。
# 最小权限改为「限定在业务库 DB_NAME」的库级授权；DDL 仅 ea_migrate / ea_sync。

case "${DB_NAME}" in
  *[!A-Za-z0-9_]*|'')
    echo "DB_NAME 只能包含字母、数字和下划线" >&2
    exit 1
    ;;
esac

escape_sql_string() {
  printf '%s' "$1" | sed "s/\\\\/\\\\\\\\/g; s/'/''/g"
}

APP_PASSWORD_ESCAPED="$(escape_sql_string "${DB_APP_PASSWORD}")"
AI_PASSWORD_ESCAPED="$(escape_sql_string "${DB_AI_PASSWORD}")"
SYNC_PASSWORD_ESCAPED="$(escape_sql_string "${DB_SYNC_PASSWORD}")"
EVENT_PASSWORD_ESCAPED="$(escape_sql_string "${DB_EVENT_PASSWORD}")"
AUTH_PASSWORD_ESCAPED="$(escape_sql_string "${DB_AUTH_PASSWORD}")"
MIGRATE_PASSWORD_ESCAPED="$(escape_sql_string "${DB_MIGRATE_PASSWORD:-${DB_SYNC_PASSWORD}}")"

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

mysql --protocol=TCP -h mysql -uroot <<SQL
CREATE USER IF NOT EXISTS 'ea_app'@'%' IDENTIFIED BY '${APP_PASSWORD_ESCAPED}';
ALTER USER 'ea_app'@'%' IDENTIFIED BY '${APP_PASSWORD_ESCAPED}';
GRANT SELECT ON \`${DB_NAME}\`.* TO 'ea_app'@'%';

CREATE USER IF NOT EXISTS 'ea_ai'@'%' IDENTIFIED BY '${AI_PASSWORD_ESCAPED}';
ALTER USER 'ea_ai'@'%' IDENTIFIED BY '${AI_PASSWORD_ESCAPED}';
GRANT SELECT ON \`${DB_NAME}\`.* TO 'ea_ai'@'%';

CREATE USER IF NOT EXISTS 'ea_sync'@'%' IDENTIFIED BY '${SYNC_PASSWORD_ESCAPED}';
ALTER USER 'ea_sync'@'%' IDENTIFIED BY '${SYNC_PASSWORD_ESCAPED}';
-- 仅显式导入脚本使用：staging 建表、原子换表与备份表需要 DDL。
-- 不再授予 FILE；导入改为应用侧批量 INSERT，不依赖 LOAD DATA。
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX
  ON \`${DB_NAME}\`.* TO 'ea_sync'@'%';

CREATE USER IF NOT EXISTS 'ea_events'@'%' IDENTIFIED BY '${EVENT_PASSWORD_ESCAPED}';
ALTER USER 'ea_events'@'%' IDENTIFIED BY '${EVENT_PASSWORD_ESCAPED}';
-- 事件写账号：仅业务库，无 DDL；表级细分在 schema 就绪后可选收紧
GRANT SELECT, INSERT, UPDATE ON \`${DB_NAME}\`.* TO 'ea_events'@'%';

CREATE USER IF NOT EXISTS 'ea_migrate'@'%' IDENTIFIED BY '${MIGRATE_PASSWORD_ESCAPED}';
ALTER USER 'ea_migrate'@'%' IDENTIFIED BY '${MIGRATE_PASSWORD_ESCAPED}';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES
  ON \`${DB_NAME}\`.* TO 'ea_migrate'@'%';

-- 认证账号在 bootstrap 阶段先建用户；表级授权由 db-migrate 之后的 apply-grants.sh 完成。
CREATE USER IF NOT EXISTS 'ea_auth'@'%' IDENTIFIED BY '${AUTH_PASSWORD_ESCAPED}';
ALTER USER 'ea_auth'@'%' IDENTIFIED BY '${AUTH_PASSWORD_ESCAPED}';

FLUSH PRIVILEGES;
SQL

# 旧数据卷升级：移除历史授予的 FILE 权限；未授予时忽略错误。
mysql --protocol=TCP -h mysql -uroot \
  -e "REVOKE FILE ON *.* FROM 'ea_sync'@'%';" >/dev/null 2>&1 || true

echo "数据库最小权限账号已就绪"
