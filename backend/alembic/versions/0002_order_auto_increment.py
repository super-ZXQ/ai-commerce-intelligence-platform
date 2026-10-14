"""为历史订单主键补齐数据库自增，保留现有ID和全部数据。"""
from alembic import op

revision = "0002_order_auto_increment"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    extra = bind.exec_driver_sql(
        "SELECT EXTRA FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='orders' AND COLUMN_NAME='order_seq_id'"
    ).scalar_one()
    if "auto_increment" not in extra.lower():
        # MySQL基于存量最大ID继续分配，不回填、不重排历史记录。
        op.execute("ALTER TABLE orders MODIFY COLUMN order_seq_id INT NOT NULL AUTO_INCREMENT")


def downgrade() -> None:
    op.execute("ALTER TABLE orders MODIFY COLUMN order_seq_id INT NOT NULL")
