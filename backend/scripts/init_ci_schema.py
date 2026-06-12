"""CI 专用：初始化 MySQL schema + 最小 seed。

为什么独立脚本：
- 不依赖 PYTHONPATH 环境变量（CI runner 上常失效）
- 不依赖 working-directory（python -m 把 cwd 加 sys.path[0]，
  但 from backend.xxx 还需要父目录在 path）
- 脚本第一件事就是显式 sys.path.insert(repo root)，万无一失
"""
import sys
from pathlib import Path

# 显式注入 repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from datetime import datetime, date
from decimal import Decimal

import pymysql
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.config import get_settings
from backend.database import Base
from backend.models.database_models import Order  # noqa: F401 注册 ORM


def main() -> None:
    s = get_settings()
    # 1. 确保 database 存在
    conn = pymysql.connect(host=s.db_host, port=s.db_port,
                           user=s.db_user, password=s.db_password)
    with conn.cursor() as c:
        c.execute(
            f"CREATE DATABASE IF NOT EXISTS `{s.db_name}` "
            f"DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    conn.commit()
    conn.close()
    print("OK database ready")

    # 2. 用 SQLAlchemy ORM 同步建表
    eng = create_engine(s.database_url)
    Base.metadata.create_all(eng)
    with eng.connect() as c:
        c.execute(text("SELECT 1"))
    print("OK tables created")

    # 3. 最小 seed：5 条 fake orders
    #    让 test_sales_overview 的 total_sales > 0 / total_orders > 0 断言通过
    #    幂等：先清空再插入
    with eng.begin() as c:
        c.execute(text("DELETE FROM orders"))
    Sess = sessionmaker(bind=eng)
    sess = Sess()
    for i in range(1, 6):
        sess.add(Order(
            # P6.11 修复 v3：ORM Python 属性名是 order_no，列名是 order_id
            # 之前用 order_id=... → TypeError: 'order_id' is an invalid keyword
            order_no=f"CI{i:05d}",
            user_name=f"user_{i % 3}",
            product_id=f"P{i:03d}",
            order_amount=Decimal("100.00"),
            payment_amount=Decimal("95.00"),
            channel_id="ch1",
            platform_type="APP" if i % 2 else "Web网站",
            order_time=datetime(2026, 6, 1, 10, 0, 0),
            payment_time=datetime(2026, 6, 1, 10, 5, 0),
            # P6.11 修复 v3：ORM Python 属性是 is_refunded（列名是 is_refund）
            is_refunded="否",
            discount_amount=Decimal("5.00"),
            payment_duration_sec=300,
            order_date=date(2026, 6, 1),
            order_hour=10,
            weekday="Monday",
        ))
    sess.commit()
    sess.close()
    print("OK 5 fake orders seeded")


if __name__ == "__main__":
    main()
