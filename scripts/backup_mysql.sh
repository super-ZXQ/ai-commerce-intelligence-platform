#!/usr/bin/env bash
# MySQL 一致性逻辑备份。
#
# 通过 mysql 容器内的 MYSQL_ROOT_PASSWORD 执行 mysqldump，密码不经过宿主机
# 命令行参数；--single-transaction 保证 InnoDB 表在同一快照内导出，不锁业务表。
#
# 用法：
#   scripts/backup_mysql.sh [--output-dir DIR] [--prune-days N]
#
# 输出：
#   stdout 只打印备份文件绝对路径（便于脚本串联）；
#   日志写 stderr；同时生成 <file>.sha256 校验文件。
#
# 注意：
#   - 备份包含业务数据与认证表，目录权限默认 0700/0600，禁止提交到 Git。
#   - 该备份是逻辑备份；未配置 binlog，RPO 等于备份间隔（默认建议每日）。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"
PRUNE_DAYS=0

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output-dir)
            BACKUP_DIR="${2:?--output-dir 需要目录参数}"
            shift 2
            ;;
        --prune-days)
            PRUNE_DAYS="${2:?--prune-days 需要天数}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if ! [[ "$PRUNE_DAYS" =~ ^[0-9]+$ ]]; then
    echo "--prune-days 必须是非负整数" >&2
    exit 2
fi

command -v docker >/dev/null 2>&1 || {
    echo "未找到 docker 命令" >&2
    exit 2
}

if ! docker compose ps --status running --services 2>/dev/null | grep -qx mysql; then
    echo "mysql 服务未运行，请先启动：docker compose up -d mysql" >&2
    exit 2
fi

db_name="$(docker compose exec -T mysql sh -c 'printf %s "$MYSQL_DATABASE"')"
if [[ -z "$db_name" ]]; then
    echo "无法从 mysql 容器读取 MYSQL_DATABASE" >&2
    exit 1
fi

umask 077
mkdir -p "$BACKUP_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_file="$BACKUP_DIR/${db_name}-${timestamp}.sql.gz"

echo "开始备份 ${db_name} -> ${backup_file}" >&2

# 密码只在容器内展开；宿主机进程列表与 shell history 都不出现明文。
if ! docker compose exec -T mysql sh -c '
    exec env MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot \
        --single-transaction --quick --routines --triggers --events \
        --set-gtid-purged=OFF --no-tablespaces --default-character-set=utf8mb4 \
        "$MYSQL_DATABASE"
' | gzip -9 >"$backup_file"; then
    rm -f "$backup_file"
    echo "备份失败，已删除不完整文件" >&2
    exit 1
fi

if ! gzip -t "$backup_file" 2>/dev/null; then
    rm -f "$backup_file"
    echo "备份文件 gzip 校验失败" >&2
    exit 1
fi

if ! gzip -dc "$backup_file" | tail -n 20 | grep -q "Dump completed"; then
    rm -f "$backup_file"
    echo "备份文件缺少 mysqldump 完成标记（可能被截断）" >&2
    exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$backup_file" >"$backup_file.sha256"
else
    shasum -a 256 "$backup_file" >"$backup_file.sha256"
fi

if (( PRUNE_DAYS > 0 )); then
    # 仅清理本库命名规则的旧备份；显式传入 --prune-days 才会执行。
    find "$BACKUP_DIR" -maxdepth 1 -type f \
        \( -name "${db_name}-*.sql.gz" -o -name "${db_name}-*.sql.gz.sha256" \) \
        -mtime +"$PRUNE_DAYS" -print -delete >&2
fi

size="$(du -h "$backup_file" | awk '{print $1}')"
echo "备份完成：${backup_file}（${size}）" >&2
printf '%s\n' "$backup_file"
