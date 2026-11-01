"""CI schema sanity：Alembic upgrade 之后校验核心表与版本号。

不负责建表；建表只走 alembic upgrade head。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from backend.config import get_settings


def main() -> None:
    expected_head = ScriptDirectory.from_config(
        Config(str(_BACKEND / "alembic.ini"))
    ).get_current_head()
    if expected_head is None:
        raise SystemExit("schema sanity failed: no alembic head revision")

    s = get_settings()
    eng = create_engine(s.database_url)
    with eng.connect() as c:
        tables = {r[0] for r in c.execute(text("SHOW TABLES")).fetchall()}
        need = {
            "orders",
            "order_events",
            "app_metadata",
            "alembic_version",
            "data_state",
            "data_import_runs",
            "order_status_reconciliation",
            "auth_users",
            "service_clients",
            "auth_audit_log",
            "ai_query_audit",
        }
        missing = need - tables
        if missing:
            raise SystemExit(f"schema sanity failed, missing tables: {sorted(missing)}")
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if ver != expected_head:
            raise SystemExit(
                f"unexpected alembic_version: {ver!r}, expected {expected_head!r}"
            )
    eng.dispose()
    print(f"OK alembic schema sanity ({expected_head})")


if __name__ == "__main__":
    main()
