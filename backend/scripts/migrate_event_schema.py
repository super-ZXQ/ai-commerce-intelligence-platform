"""为已有本地 MySQL 数据卷补齐事件表与订单状态列。

Docker 空卷会直接执行 sql/01_create_table.sql；已有开发数据卷不会重新执行 initdb，
因此仅在明确运行本脚本时做幂等 schema 迁移。它不读取、删除或重导 CSV 数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine, text

from backend.config import get_settings
from backend.database import Base
from backend.models import database_models  # noqa: F401 - 注册全部 ORM 表


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    additions = {
        "order_status": "VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'",
        "event_version": "INT NOT NULL DEFAULT 0",
        "updated_at": "DATETIME NULL",
    }
    with engine.begin() as connection:
        columns = {
            row[0]
            for row in connection.execute(text("SHOW COLUMNS FROM orders"))
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE orders ADD COLUMN {name} {definition}"))
        connection.execute(text("UPDATE orders SET order_status = 'ACTIVE' WHERE order_status IS NULL"))
        connection.execute(text("UPDATE orders SET event_version = 0 WHERE event_version IS NULL"))
        # 历史 Docker 卷可能没有 AUTO_INCREMENT；事件路径也会显式分配主键，这里双保险对齐 schema。
        extra = connection.execute(
            text(
                "SELECT EXTRA FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders' "
                "AND COLUMN_NAME = 'order_seq_id'"
            )
        ).scalar()
        if extra is None or "auto_increment" not in str(extra).lower():
            connection.execute(
                text("ALTER TABLE orders MODIFY order_seq_id INT NOT NULL AUTO_INCREMENT")
            )
    engine.dispose()
    print("OK event schema ready; existing orders retained as ACTIVE/version=0")


if __name__ == "__main__":
    main()
