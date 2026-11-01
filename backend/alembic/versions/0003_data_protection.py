"""数据保护：导入审计、数据版本、状态对账审计。

Revision ID: 0003_data_protection
Revises: 0002_order_auto_increment
Create Date: 2026-11-01

本迁移只新增审计/版本表，不修改 orders 业务数据：
- data_state：全局数据版本行（单行 id=1），供缓存键与快照一致性使用；
- data_import_runs：每次 CSV 导入的哈希、模式、行数与结果；
- order_status_reconciliation：历史退款状态对账的逐订单审计。

导入脚本在原子换表成功后才会写入 completed；失败时保留 failed 审计，
原 orders 表不变。禁止通过本迁移自动改写任何订单状态。
"""
from __future__ import annotations

from alembic import op

revision = "0003_data_protection"
down_revision = "0002_order_auto_increment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS data_state (
            id TINYINT UNSIGNED NOT NULL,
            data_version BIGINT UNSIGNED NOT NULL DEFAULT 1,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    op.execute(
        "INSERT INTO data_state (id, data_version) VALUES (1, 1) "
        "ON DUPLICATE KEY UPDATE id = id"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS data_import_runs (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            source VARCHAR(512) NOT NULL,
            source_sha256 CHAR(64) NOT NULL,
            mode VARCHAR(20) NOT NULL,
            status VARCHAR(20) NOT NULL,
            operator VARCHAR(100) NOT NULL,
            rows_read INT UNSIGNED NOT NULL DEFAULT 0,
            rows_imported INT UNSIGNED NOT NULL DEFAULT 0,
            backup_table VARCHAR(64) NULL,
            started_at DATETIME NOT NULL,
            finished_at DATETIME NULL,
            error_message TEXT NULL,
            notes TEXT NULL,
            PRIMARY KEY (id),
            KEY idx_data_import_runs_started (started_at),
            KEY idx_data_import_runs_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS order_status_reconciliation (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            run_id BIGINT UNSIGNED NOT NULL,
            order_id VARCHAR(50) NOT NULL,
            old_status VARCHAR(20) NOT NULL,
            new_status VARCHAR(20) NOT NULL,
            reason VARCHAR(255) NOT NULL,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            KEY idx_reconciliation_run (run_id),
            KEY idx_reconciliation_order (order_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


def downgrade() -> None:
    # 只回滚审计表；orders 业务数据不受影响。
    op.execute("DROP TABLE IF EXISTS order_status_reconciliation")
    op.execute("DROP TABLE IF EXISTS data_import_runs")
    op.execute("DROP TABLE IF EXISTS data_state")
