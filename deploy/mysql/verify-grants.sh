#!/bin/sh
# 最小权限账号验收：用真实账号连接 MySQL，验证允许与禁止的语句。
#
# 由 docker compose --profile verify run --rm --no-deps db-verify 执行；
# 只做只读与 0 行更新/删除探测，不修改业务数据。
set -eu

DB_HOST="${DB_HOST:-mysql}"
DB_NAME="${DB_NAME:?DB_NAME 未设置}"

: "${DB_APP_PASSWORD:?DB_APP_PASSWORD 未设置}"
: "${DB_AI_PASSWORD:?DB_AI_PASSWORD 未设置}"
: "${DB_EVENT_PASSWORD:?DB_EVENT_PASSWORD 未设置}"
: "${DB_AUTH_PASSWORD:?DB_AUTH_PASSWORD 未设置}"

fail=0

run_sql() {
    account="$1"
    password="$2"
    sql="$3"
    MYSQL_PWD="$password" mysql -h "$DB_HOST" -u "$account" -Nse "$sql" "$DB_NAME"
}

expect_ok() {
    desc="$1"
    account="$2"
    password="$3"
    sql="$4"
    if run_sql "$account" "$password" "$sql" >/dev/null 2>&1; then
        echo "PASS  $desc"
    else
        echo "FAIL  $desc（应成功但被拒绝）" >&2
        fail=1
    fi
}

expect_denied() {
    desc="$1"
    account="$2"
    password="$3"
    sql="$4"
    if run_sql "$account" "$password" "$sql" >/dev/null 2>&1; then
        echo "FAIL  $desc（应被拒绝但执行成功）" >&2
        fail=1
    else
        echo "PASS  $desc"
    fi
}

# ── ea_app：业务读路径，只读业务表 ──
expect_ok     "ea_app 读取 orders"            ea_app "$DB_APP_PASSWORD" "SELECT COUNT(*) FROM orders"
expect_ok     "ea_app 读取 data_state"        ea_app "$DB_APP_PASSWORD" "SELECT COUNT(*) FROM data_state"
expect_denied "ea_app 不能更新 orders"        ea_app "$DB_APP_PASSWORD" "UPDATE orders SET updated_at = updated_at WHERE 1 = 0"
expect_denied "ea_app 不能读取 auth_users"    ea_app "$DB_APP_PASSWORD" "SELECT COUNT(*) FROM auth_users"

# ── ea_ai：Text-to-SQL 只读，仅 orders ──
expect_ok     "ea_ai 读取 orders"             ea_ai "$DB_AI_PASSWORD" "SELECT COUNT(*) FROM orders"
expect_denied "ea_ai 不能读取 data_state"     ea_ai "$DB_AI_PASSWORD" "SELECT COUNT(*) FROM data_state"
expect_denied "ea_ai 不能读取 auth_users"     ea_ai "$DB_AI_PASSWORD" "SELECT COUNT(*) FROM auth_users"
expect_denied "ea_ai 不能更新 orders"         ea_ai "$DB_AI_PASSWORD" "UPDATE orders SET updated_at = updated_at WHERE 1 = 0"

# ── ea_events：订单/事件/数据版本写入 ──
expect_ok     "ea_events 读取 orders"         ea_events "$DB_EVENT_PASSWORD" "SELECT COUNT(*) FROM orders"
expect_ok     "ea_events 可递增 data_version" ea_events "$DB_EVENT_PASSWORD" "UPDATE data_state SET data_version = data_version WHERE id = 1"
expect_ok     "ea_events 可更新 orders"       ea_events "$DB_EVENT_PASSWORD" "UPDATE orders SET updated_at = updated_at WHERE 1 = 0"
expect_denied "ea_events 不能删除 data_state" ea_events "$DB_EVENT_PASSWORD" "DELETE FROM data_state WHERE 1 = 0"
expect_denied "ea_events 不能删除 orders"     ea_events "$DB_EVENT_PASSWORD" "DELETE FROM orders WHERE 1 = 0"
expect_denied "ea_events 不能读取 auth_users" ea_events "$DB_EVENT_PASSWORD" "SELECT COUNT(*) FROM auth_users"

# ── ea_auth：认证/审计表，无业务数据权限 ──
expect_ok     "ea_auth 读取 auth_users"       ea_auth "$DB_AUTH_PASSWORD" "SELECT COUNT(*) FROM auth_users"
expect_denied "ea_auth 不能读取 orders"       ea_auth "$DB_AUTH_PASSWORD" "SELECT COUNT(*) FROM orders"
expect_denied "ea_auth 不能删除审计日志"      ea_auth "$DB_AUTH_PASSWORD" "DELETE FROM auth_audit_log WHERE 1 = 0"

if [ "$fail" -ne 0 ]; then
    echo "权限验收失败：存在与 docs/DB_PERMISSIONS.md 不一致的授权" >&2
    exit 1
fi

echo "权限验收通过：各账号仅拥有文档声明的权限"
