"""业务数据导入：运行计数、确认字段与逐行暂存。

Revision ID: 0007_data_import
Revises: 0006_episode_memory
Create Date: 2026-11-06

设计要点：
- 复用既有 data_import_runs 作为草稿/确认/执行/审计的统一头表；
- 新增 data_import_rows 保存规范化后的暂存行与失败明细，确认后无需重新上传文件；
- 所有新增计数列都有默认值，旧 CLI（bootstrap/replace）不受影响；
- upgrade/downgrade 都按 information_schema 判断差异，重复执行不会重复加列；
- downgrade 删除新表与新增列，可完整回滚。
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0007_data_import"
down_revision = "0006_episode_memory"
branch_labels = None
depends_on = None

_NEW_RUN_COLUMNS = (
    "source_type VARCHAR(10) NOT NULL DEFAULT 'csv'",
    "original_filename VARCHAR(255) NOT NULL DEFAULT ''",
    "source_size_bytes BIGINT UNSIGNED NOT NULL DEFAULT 0",
    "options_json JSON NULL",
    "conflict_policy VARCHAR(20) NOT NULL DEFAULT 'reject'",
    "rows_valid INT UNSIGNED NOT NULL DEFAULT 0",
    "rows_conflict INT UNSIGNED NOT NULL DEFAULT 0",
    "rows_invalid INT UNSIGNED NOT NULL DEFAULT 0",
    "rows_skipped INT UNSIGNED NOT NULL DEFAULT 0",
    "rows_rejected INT UNSIGNED NOT NULL DEFAULT 0",
    "rows_inserted INT UNSIGNED NOT NULL DEFAULT 0",
    "rows_refunded INT UNSIGNED NOT NULL DEFAULT 0",
    "data_version_before INT UNSIGNED NULL",
    "data_version_after INT UNSIGNED NULL",
    "confirmed_by VARCHAR(100) NOT NULL DEFAULT ''",
    "confirmed_at DATETIME NULL",
    "confirm_token_hash CHAR(64) NOT NULL DEFAULT ''",
    "heartbeat_at DATETIME NULL",
    "error_code VARCHAR(64) NOT NULL DEFAULT ''",
)


def upgrade() -> None:
    # MySQL 8 不支持 ADD COLUMN IF NOT EXISTS；用 information_schema 只补缺失列，
    # 保证迁移可在已升级的库上重复执行（测试与手工排查会重放 head）。
    missing = [
        definition
        for definition in _NEW_RUN_COLUMNS
        if definition.split(" ", 1)[0].lower() not in _existing_run_columns()
    ]
    if missing:
        op.execute(
            "ALTER TABLE data_import_runs ADD COLUMN " + ", ADD COLUMN ".join(missing)
        )
    if not _table_exists("data_import_rows"):
        op.execute(
            """
            CREATE TABLE data_import_rows (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                run_id BIGINT UNSIGNED NOT NULL,
                row_no INT UNSIGNED NOT NULL,
                order_id VARCHAR(50) NOT NULL DEFAULT '',
                status VARCHAR(20) NOT NULL,
                error_code VARCHAR(64) NOT NULL DEFAULT '',
                error_message VARCHAR(500) NOT NULL DEFAULT '',
                normalized_json JSON NULL,
                raw_json JSON NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uq_data_import_rows_run_row (run_id, row_no),
                KEY idx_data_import_rows_run_status (run_id, status),
                KEY idx_data_import_rows_order (order_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
    else:
        # 早期版本没写 COLLATE，MySQL 8 会落到 utf8mb4_0900_ai_ci；与 orders/
        # order_events 的 utf8mb4_unicode_ci 做等值连接会报 Illegal mix of collations。
        _align_rows_table_collation()


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS data_import_rows")
    present = _existing_run_columns()
    drops = [
        f"DROP COLUMN {column}"
        for column in _column_names()
        if column.lower() in present
    ]
    if drops:
        op.execute("ALTER TABLE data_import_runs " + ", ".join(drops))


def _existing_run_columns() -> set[str]:
    bind = op.get_bind()
    rows = bind.execute(
        text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'data_import_runs'"
        )
    ).fetchall()
    return {str(row[0]).lower() for row in rows}


def _align_rows_table_collation() -> None:
    """把已存在的暂存表对齐到 orders/order_events 的排序规则（幂等）。"""
    bind = op.get_bind()
    collation = bind.execute(
        text(
            "SELECT TABLE_COLLATION FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'data_import_rows'"
        )
    ).scalar()
    if collation and str(collation).lower() != "utf8mb4_unicode_ci":
        op.execute(
            "ALTER TABLE data_import_rows "
            "CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    count = bind.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :name"
        ),
        {"name": table_name},
    ).scalar()
    return bool(count)


def _column_names() -> list[str]:
    """从列定义中提取列名，避免 downgrade 时重复维护列清单。"""
    return [definition.split(" ", 1)[0] for definition in _NEW_RUN_COLUMNS]
