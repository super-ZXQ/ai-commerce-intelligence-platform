"""为历史 orders 表启用自增主键，避免并发创建竞争。"""
from alembic import op

revision = "0002_order_auto_increment"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    extra = bind.exec_driver_sql(
        "SELECT EXTRA FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders' "
        "AND COLUMN_NAME = 'order_seq_id'"
    ).scalar()
    if extra is None:
        raise RuntimeError("orders.order_seq_id 不存在，无法启用自增")
    if "auto_increment" not in extra.lower():
        bind.exec_driver_sql("ALTER TABLE orders MODIFY order_seq_id INT NOT NULL AUTO_INCREMENT")


def downgrade() -> None:
    # 保留自增，避免回滚后运行中的写入路径无法分配主键。
    pass
