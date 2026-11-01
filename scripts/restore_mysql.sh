#!/usr/bin/env bash
# 将 mysqldump 备份恢复到 MySQL。
#
# 安全默认值：
#   - 默认恢复到 <生产库>_restore_drill，不覆盖生产库；
#   - 目标库非空时拒绝写入，除非显式 --allow-overwrite；
#   - 自动校验相邻的 .sha256 文件（存在时）。
#
# 用法：
#   scripts/restore_mysql.sh <backup.sql.gz> [--target-db NAME]
#                            [--allow-overwrite] [--skip-checksum]
#
# 恢复到生产库属于破坏性操作，必须先停止写入并完成独立备份。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DUMP_FILE=""
TARGET_DB=""
ALLOW_OVERWRITE=0
SKIP_CHECKSUM=0

usage() {
    sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-db)
            TARGET_DB="${2:?--target-db 需要库名}"
            shift 2
            ;;
        --allow-overwrite)
            ALLOW_OVERWRITE=1
            shift
            ;;
        --skip-checksum)
            SKIP_CHECKSUM=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [[ -n "$DUMP_FILE" ]]; then
                echo "只能指定一个备份文件" >&2
                exit 2
            fi
            DUMP_FILE="$1"
            shift
            ;;
    esac
done

[[ -n "$DUMP_FILE" ]] || {
    echo "缺少备份文件参数" >&2
    usage >&2
    exit 2
}
[[ -f "$DUMP_FILE" ]] || {
    echo "备份文件不存在：$DUMP_FILE" >&2
    exit 2
}

command -v docker >/dev/null 2>&1 || {
    echo "未找到 docker 命令" >&2
    exit 2
}

if ! docker compose ps --status running --services 2>/dev/null | grep -qx mysql; then
    echo "mysql 服务未运行" >&2
    exit 2
fi

source_db="$(docker compose exec -T mysql sh -c 'printf %s "$MYSQL_DATABASE"')"
if [[ -z "$source_db" ]]; then
    echo "无法从 mysql 容器读取 MYSQL_DATABASE" >&2
    exit 1
fi

target_db="${TARGET_DB:-${source_db}_restore_drill}"
if ! [[ "$target_db" =~ ^[A-Za-z0-9_]{1,64}$ ]]; then
    echo "目标库名只能包含字母、数字和下划线，最长 64 字符" >&2
    exit 2
fi

if [[ "$target_db" == "$source_db" && "$ALLOW_OVERWRITE" -ne 1 ]]; then
    echo "拒绝覆盖生产库 ${source_db}；如确认执行，请显式传入 --allow-overwrite" >&2
    exit 2
fi

if ! gzip -t "$DUMP_FILE" 2>/dev/null; then
    echo "备份文件 gzip 校验失败：$DUMP_FILE" >&2
    exit 1
fi

if (( SKIP_CHECKSUM == 0 )); then
    if [[ -f "${DUMP_FILE}.sha256" ]]; then
        echo "校验备份完整性..." >&2
        if command -v sha256sum >/dev/null 2>&1; then
            ( cd "$(dirname "$DUMP_FILE")" && sha256sum -c "$(basename "$DUMP_FILE").sha256" ) >&2
        else
            ( cd "$(dirname "$DUMP_FILE")" && shasum -a 256 -c "$(basename "$DUMP_FILE").sha256" ) >&2
        fi
    else
        echo "未找到 ${DUMP_FILE}.sha256，跳过完整性校验" >&2
    fi
fi

# SQL 通过环境变量传入容器，避免宿主机与容器之间的多层引号转义。
run_root_sql() {
    docker compose exec -T -e TARGET_SQL="$1" mysql sh -c \
        'exec env MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -Nse "$TARGET_SQL"'
}

run_db_sql() {
    docker compose exec -T -e TARGET_DB="$1" -e TARGET_SQL="$2" mysql sh -c \
        'exec env MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot --database="$TARGET_DB" -Nse "$TARGET_SQL"'
}

echo "创建目标库 ${target_db}（如不存在）..." >&2
run_root_sql "CREATE DATABASE IF NOT EXISTS \`${target_db}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" >/dev/null

table_count="$(run_root_sql "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '${target_db}'")"
if [[ "$table_count" != "0" && "$ALLOW_OVERWRITE" -ne 1 ]]; then
    echo "目标库 ${target_db} 已有 ${table_count} 张表；拒绝覆盖。请换库名或显式 --allow-overwrite" >&2
    exit 2
fi

echo "恢复 ${DUMP_FILE} -> ${target_db} ..." >&2
gzip -dc "$DUMP_FILE" | docker compose exec -T -e TARGET_DB="$target_db" mysql sh -c \
    'exec env MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot --database="$TARGET_DB"'

echo "恢复完成，关键表行数：" >&2
for table in orders order_events auth_users data_state alembic_version; do
    count="$(run_db_sql "$target_db" "SELECT COUNT(*) FROM ${table}")"
    printf '%s=%s\n' "$table" "$count"
done
