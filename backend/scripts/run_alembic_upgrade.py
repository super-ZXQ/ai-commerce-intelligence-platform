"""CI entry: run Alembic upgrade head from repo root via command API.

Avoids `python -m alembic` (package/dir name collision risk) and prints
enough context to debug CI failures.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
_INI = _BACKEND / "alembic.ini"

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def main() -> int:
    print(f"python={sys.executable}")
    print(f"cwd_root={_ROOT}")
    print(f"alembic_ini={_INI} exists={_INI.is_file()}")
    print(f"sys.path[0:3]={sys.path[:3]}")
    try:
        import alembic
        from alembic import command
        from alembic.config import Config

        print(f"alembic_file={getattr(alembic, '__file__', None)}")
        print(f"alembic_version={getattr(alembic, '__version__', 'unknown')}")
    except Exception:
        traceback.print_exc()
        print("FAIL: cannot import alembic")
        return 1

    if not _INI.is_file():
        print(f"FAIL: missing {_INI}")
        return 1

    cfg = Config(str(_INI))
    try:
        command.upgrade(cfg, "head")
        print("OK alembic upgrade head")
        command.current(cfg)
        print("OK alembic current")
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        print(f"alembic_heads={heads}")
        if heads != ["0001_baseline"]:
            print(f"FAIL: unexpected heads {heads}")
            return 1
    except Exception:
        traceback.print_exc()
        print("FAIL: alembic upgrade")
        return 1

    # schema sanity
    try:
        from sqlalchemy import create_engine, text

        from backend.config import get_settings

        s = get_settings()
        print(f"db_host={s.db_host} db_name={s.db_name} db_user={s.db_user}")
        eng = create_engine(s.database_url)
        with eng.connect() as c:
            tables = {r[0] for r in c.execute(text("SHOW TABLES")).fetchall()}
            need = {"orders", "order_events", "app_metadata", "alembic_version"}
            missing = need - tables
            if missing:
                print(f"FAIL: missing tables {sorted(missing)}; have={sorted(tables)}")
                return 1
            ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            if ver != "0001_baseline":
                print(f"FAIL: unexpected alembic_version {ver!r}")
                return 1
        eng.dispose()
        print("OK alembic schema sanity")
    except Exception:
        traceback.print_exc()
        print("FAIL: schema sanity")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
