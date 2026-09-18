"""CI/本地兼容入口：Alembic 建 schema + seed 测试数据。

主路径：
  python -m alembic -c backend/alembic.ini upgrade head
  python backend/scripts/seed_ci_data.py

本脚本保留给习惯旧命令的本地开发；CI 已拆成 alembic + seed 两步。
不再使用 Base.metadata.create_all 作为 schema 权威路径。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO_ROOT / "backend"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pymysql  # noqa: E402

from backend.config import get_settings  # noqa: E402
from backend.scripts.seed_ci_data import main as seed_main  # noqa: E402


def _ensure_database() -> None:
    s = get_settings()
    conn = pymysql.connect(host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password)
    with conn.cursor() as c:
        c.execute(
            f"CREATE DATABASE IF NOT EXISTS `{s.db_name}` "
            f"DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    conn.commit()
    conn.close()
    print("OK database ready")


def _run_alembic_upgrade() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(_REPO_ROOT), env.get("PYTHONPATH", "")])
    )
    # 必须从仓库根执行：cwd=backend 时 backend/alembic/ 会遮蔽 site-packages 的 alembic
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(_BACKEND / "alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        check=True,
    )
    print("OK alembic upgrade head")


def main() -> None:
    _ensure_database()
    _run_alembic_upgrade()
    seed_main()


if __name__ == "__main__":
    main()
