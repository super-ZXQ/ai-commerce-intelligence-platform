"""CI seed：只写入测试用最小业务数据，不负责建表。

Schema 由 Alembic 管理：
    python -m alembic -c backend/alembic.ini upgrade head
    python scripts/seed_ci_data.py
"""
from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.config import get_settings
from backend.models.database_models import Order


def seed_ci_orders(engine) -> int:
    """幂等：清空 orders 后插入 5 条 CI 假订单。"""
    with engine.begin() as c:
        c.execute(text("DELETE FROM orders"))
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        for i in range(1, 6):
            session.add(Order(
                order_no=f"CI{i:05d}",
                user_name=f"user_{i % 3}",
                product_id=f"P{i:03d}",
                order_amount=Decimal("100.00"),
                payment_amount=Decimal("95.00"),
                channel_id="ch1",
                platform_type="APP" if i % 2 else "Web网站",
                order_time=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
                payment_time=datetime(2026, 6, 1, 10, 5, 0, tzinfo=UTC),
                is_refunded="否",
                discount_amount=Decimal("5.00"),
                payment_duration_sec=300,
                order_date=date(2026, 6, 1),
                order_hour=10,
                weekday="Monday",
            ))
        session.commit()
    finally:
        session.close()
    return 5


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    # schema 必须已由 Alembic 创建；此处只校验表存在
    with engine.connect() as c:
        c.execute(text("SELECT 1 FROM orders LIMIT 1"))
    count = seed_ci_orders(engine)
    engine.dispose()
    print(f"OK {count} CI orders seeded (schema must come from alembic upgrade head)")


if __name__ == "__main__":
    main()
