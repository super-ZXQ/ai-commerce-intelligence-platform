"""CI schema sanity：Alembic upgrade 之后校验核心表与版本号。

不负责建表；建表只走 alembic upgrade head。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import create_engine, text

from backend.config import get_settings


def main() -> None:
    s = get_settings()
    eng = create_engine(s.database_url)
    with eng.connect() as c:
        tables = {r[0] for r in c.execute(text("SHOW TABLES")).fetchall()}
        need = {"orders", "order_events", "app_metadata", "alembic_version"}
        missing = need - tables
        if missing:
            raise SystemExit(f"schema sanity failed, missing tables: {sorted(missing)}")
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if ver != "0001_baseline":
            raise SystemExit(f"unexpected alembic_version: {ver!r}")
    eng.dispose()
    print("OK alembic schema sanity")


if __name__ == "__main__":
    main()
