#!/usr/bin/env bash
# 备份 + 恢复演练：备份生产库 → 恢复到独立临时库 → 对比关键表行数。
#
# 这是验收“备份可用”的唯一证据来源。仅生成备份文件不算完成恢复演练。
#
# 用法：
#   scripts/backup_restore_drill.sh [--keep]
#
#   --keep  保留临时库与备份文件，便于人工核查（默认结束后删除）。
#
# 默认行为：
#   - 临时库名 drill_<UTC 时间戳>，不触碰生产库；
#   - 演练期间应停止订单事件写入，否则行数对比可能因并发写入漂移；
#   - 失败时返回非零退出码。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

KEEP=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep)
            KEEP=1
            shift
            ;;
        -h|--help)
            sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "未知参数：$1" >&2
            exit 2
            ;;
    esac
done

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

scratch_db="drill_$(date -u +%Y%m%d%H%M%S)"
if ! [[ "$scratch_db" =~ ^drill_[0-9]{14}$ ]]; then
    echo "临时库名生成异常：$scratch_db" >&2
    exit 1
fi

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/ea-backup-drill.XXXXXX")"
dump_file=""

run_root_sql() {
    docker compose exec -T -e TARGET_SQL="$1" mysql sh -c \
        'exec env MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -Nse "$TARGET_SQL"'
}

run_db_sql() {
    docker compose exec -T -e TARGET_DB="$1" -e TARGET_SQL="$2" mysql sh -c \
        'exec env MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot --database="$TARGET_DB" -Nse "$TARGET_SQL"'
}

counts_for() {
    local db="$1"
    for table in orders order_events auth_users data_state alembic_version; do
        printf '%s=%s\n' "$table" "$(run_db_sql "$db" "SELECT COUNT(*) FROM ${table}")"
    done
}

cleanup() {
    local exit_code=$?
    if (( KEEP == 0 )); then
        # 只允许删除本脚本创建的临时库，避免误删其他 schema。
        if [[ "$scratch_db" == drill_* ]]; then
            run_root_sql "DROP DATABASE IF EXISTS \`${scratch_db}\`" >/dev/null 2>&1 || true
        fi
        rm -rf "$tmp_dir"
    else
        echo "已保留临时库 ${scratch_db} 与目录 ${tmp_dir}" >&2
        if [[ -n "$dump_file" ]]; then
            echo "备份文件：${dump_file}" >&2
        fi
    fi
    exit "$exit_code"
}
trap cleanup EXIT

echo "=== 1/4 备份生产库 ${source_db} ===" >&2
dump_file="$(scripts/backup_mysql.sh --output-dir "$tmp_dir")"
echo "备份文件：${dump_file}" >&2

echo "=== 2/4 读取源库行数 ===" >&2
source_counts="$(counts_for "$source_db")"
printf '%s\n' "$source_counts" >&2

echo "=== 3/4 恢复到临时库 ${scratch_db} ===" >&2
if ! restored_counts="$(scripts/restore_mysql.sh "$dump_file" \
    --target-db "$scratch_db" 2>"$tmp_dir/restore.log")"; then
    cat "$tmp_dir/restore.log" >&2
    echo "恢复失败，演练终止" >&2
    exit 1
fi
printf '%s\n' "$restored_counts" >&2

echo "=== 4/4 对比行数 ===" >&2
if [[ "$source_counts" != "$restored_counts" ]]; then
    echo "恢复演练失败：源库与恢复库行数不一致" >&2
    diff <(printf '%s\n' "$source_counts") <(printf '%s\n' "$restored_counts") >&2 || true
    exit 1
fi

echo "恢复演练通过：${source_counts//$'\n'/, }" >&2
