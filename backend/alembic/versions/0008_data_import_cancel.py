"""业务数据导入：执行取消标记。

Revision ID: 0008_data_import_cancel
Revises: 0007_data_import
Create Date: 2026-11-06

取消只设置标记，由执行任务在当前分块提交后读取；已经提交的分块保留，
未处理的行不再写入。该列可重复迁移，downgrade 完整删除。
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0008_data_import_cancel"
down_revision = "0007_data_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "cancel_requested" not in _existing_columns():
        op.execute(
            "ALTER TABLE data_import_runs "
            "ADD COLUMN cancel_requested TINYINT(1) NOT NULL DEFAULT 0"
        )


def downgrade() -> None:
    if "cancel_requested" in _existing_columns():
        op.execute("ALTER TABLE data_import_runs DROP COLUMN cancel_requested")


def _existing_columns() -> set[str]:
    bind = op.get_bind()
    rows = bind.execute(
        text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'data_import_runs'"
        )
    ).fetchall()
    return {str(row[0]).lower() for row in rows}
