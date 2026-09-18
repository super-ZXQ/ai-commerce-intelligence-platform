"""Alembic migration regression: single head, DDL consistency, MySQL checks."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND = Path(__file__).resolve().parents[1]
_ROOT = _BACKEND.parent
_ALEMBIC_INI = _BACKEND / "alembic.ini"
_VERSIONS = _BACKEND / "alembic" / "versions"
_REF_SQL = _ROOT / "sql" / "01_create_table.sql"
_SEED = _BACKEND / "scripts" / "seed_ci_data.py"
_BOOTSTRAP_SH = _ROOT / "deploy" / "mysql" / "bootstrap-users.sh"
# alembic CLI must run from repo root: backend/alembic/ can shadow the package
_ALEMBIC_CWD = _ROOT

_EXPECTED_INDEXES_ORDERS = {
    "uq_orders_order_id",
    "idx_orders_order_time",
    "idx_orders_order_date",
    "idx_orders_platform_date",
    "idx_orders_user_date",
    "idx_orders_product_id",
    "idx_orders_payment_amount",
    "idx_orders_is_refund",
    "idx_orders_status_date",
}
_EXPECTED_INDEXES_EVENTS = {
    "uq_order_events_event_id",
    "idx_order_events_order_sequence",
    "idx_order_events_status_occurred",
}


def _script() -> ScriptDirectory:
    cfg = Config(str(_ALEMBIC_INI))
    return ScriptDirectory.from_config(cfg)


def _mysql_ready() -> tuple[bool, object | None, Exception | None]:
    try:
        import pymysql

        from backend.config import get_settings

        s = get_settings()
        conn = pymysql.connect(
            host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password
        )
        conn.close()
        return True, s, None
    except Exception as exc:  # noqa: BLE001
        return False, None, exc


def _alembic(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(_ALEMBIC_INI), *args],
        cwd=str(_ALEMBIC_CWD),
        check=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(_ROOT), os.environ.get("PYTHONPATH", "")]))},
    )


def test_alembic_ini_and_versions_tree_exist():
    assert _ALEMBIC_INI.is_file()
    assert (_BACKEND / "alembic" / "env.py").is_file()
    assert _VERSIONS.is_dir()


def test_alembic_single_head_is_baseline():
    script = _script()
    heads = script.get_heads()
    assert heads == ["0001_baseline"], f"expect single head=0001_baseline, got {heads}"
    revision = script.get_revision("0001_baseline")
    assert revision.down_revision is None


def test_baseline_migration_declares_core_tables_and_indexes():
    source = (_VERSIONS / "0001_baseline.py").read_text(encoding="utf-8")
    markers = [
        "CREATE TABLE IF NOT EXISTS orders",
        "CREATE TABLE IF NOT EXISTS order_events",
        "CREATE TABLE IF NOT EXISTS app_metadata",
        "order_seq_id INT NOT NULL AUTO_INCREMENT",
        "uq_order_events_event_id",
        "idx_orders_status_date",
        "idx_orders_platform_date",
        "idx_order_events_order_sequence",
        "idx_order_events_status_occurred",
        "DEFAULT 'ACTIVE'",
        "DEFAULT 0",
    ]
    for marker in markers:
        assert marker in source, f"baseline missing: {marker}"
    assert "INSERT INTO orders" not in source
    assert "CI00001" not in source


def test_reference_sql_and_baseline_stay_consistent():
    """Reference DDL in sql/01 must not drift from Alembic baseline."""
    ref = _REF_SQL.read_text(encoding="utf-8")
    base = (_VERSIONS / "0001_baseline.py").read_text(encoding="utf-8")
    for fragment in (
        "order_seq_id INT NOT NULL AUTO_INCREMENT",
        "uq_orders_order_id",
        "idx_orders_platform_date",
        "idx_orders_status_date",
        "uq_order_events_event_id",
        "idx_order_events_order_sequence",
        "idx_order_events_status_occurred",
        "order_status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'",
        "event_version INT NOT NULL DEFAULT 0",
        "payload JSON NOT NULL",
    ):
        assert fragment in ref, f"reference SQL missing: {fragment}"
        assert fragment in base, f"baseline missing: {fragment}"
    assert "source of truth" in ref.lower() or "Alembic" in ref


def test_seed_script_contains_no_ddl():
    seed = _SEED.read_text(encoding="utf-8").upper()
    for banned in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "CREATE_ALL", "METADATA.CREATE"):
        assert banned not in seed, f"seed_ci_data must not contain DDL: {banned}"


def test_bootstrap_role_separation():
    sh = _BOOTSTRAP_SH.read_text(encoding="utf-8")
    assert "ea_ai" in sh and "GRANT SELECT" in sh
    assert "ea_migrate" in sh
    assert "ea_events" in sh
    ai_block = sh.split("ea_ai")[1].split("ea_sync")[0]
    for ddl in ("CREATE,", "DROP,", "ALTER,"):
        assert ddl not in ai_block.replace("CREATE USER", ""), "ea_ai must not get DDL grants"
    assert "ON \\`${DB_NAME}\\`.*" in sh or "ON `${DB_NAME}`.*" in sh


def test_migration_url_never_defaults_to_ai_readonly_account(monkeypatch):
    monkeypatch.setenv("MIGRATION_DB_USER", "ea_migrate")
    monkeypatch.setenv("MIGRATION_DB_PASSWORD", "migrate-secret")
    monkeypatch.setenv("AI_DB_USER", "ea_ai")
    monkeypatch.setenv("AI_DB_PASSWORD", "ai-secret")
    from backend.config import Settings

    s = Settings(
        jwt_secret="ci-test-secret-not-for-production",
        admin_password="admin123",
        debug=True,
        db_user="root",
        db_password="rootpw",
        ai_db_user="ea_ai",
        ai_db_password="ai-secret",
    )
    url = s.migration_database_url
    assert "ea_migrate" in url
    assert "migrate-secret" in url
    assert "ea_ai" not in url


def test_alembic_upgrade_head_on_mysql_if_available():
    """Force replay baseline on available MySQL; verify tables, indexes, version."""
    if os.environ.get("SKIP_MYSQL_ALEMBIC") == "1":
        pytest.skip("SKIP_MYSQL_ALEMBIC=1")
    ok, s, exc = _mysql_ready()
    if not ok:
        pytest.skip(f"MySQL unavailable: {exc}")

    import pymysql
    from sqlalchemy import create_engine, text

    conn = pymysql.connect(host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password)
    with conn.cursor() as c:
        c.execute(
            f"CREATE DATABASE IF NOT EXISTS `{s.db_name}` "
            f"DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    conn.commit()
    conn.close()

    engine = create_engine(s.database_url)
    with engine.begin() as c:
        c.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        c.execute(text("DELETE FROM alembic_version"))
    engine.dispose()

    _alembic("upgrade", "head")
    _alembic("upgrade", "head")
    _alembic("heads")

    engine = create_engine(s.database_url)
    with engine.connect() as c:
        tables = {row[0] for row in c.execute(text("SHOW TABLES")).fetchall()}
        assert {"orders", "order_events", "app_metadata", "alembic_version"} <= tables
        cols = {row[0] for row in c.execute(text("SHOW COLUMNS FROM orders")).fetchall()}
        assert {"order_seq_id", "order_id", "order_status", "event_version"} <= cols
        version = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == "0001_baseline"
        event_cols = {
            row[0] for row in c.execute(text("SHOW COLUMNS FROM order_events")).fetchall()
        }
        assert {"event_id", "processing_status", "sequence"} <= event_cols
        order_idx = {row[2] for row in c.execute(text("SHOW INDEX FROM orders")).fetchall()}
        event_idx = {row[2] for row in c.execute(text("SHOW INDEX FROM order_events")).fetchall()}
        order_id_unique = c.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='orders' "
                "AND COLUMN_NAME='order_id' AND NON_UNIQUE=0"
            )
        ).scalar()
        assert order_id_unique >= 1, "orders.order_id must have a unique index"
        required_orders = _EXPECTED_INDEXES_ORDERS - {"uq_orders_order_id"}
        missing_o = required_orders - order_idx
        missing_e = _EXPECTED_INDEXES_EVENTS - event_idx
        assert not missing_o, f"orders missing indexes: {missing_o}"
        assert not missing_e, f"order_events missing indexes: {missing_e}"
        extra = c.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='orders' "
                "AND COLUMN_NAME='audit_note'"
            )
        ).scalar()
        assert not extra
    engine.dispose()


def test_migration_preserves_existing_rows_on_mysql_if_available():
    """upgrade head must not wipe business rows."""
    if os.environ.get("SKIP_MYSQL_ALEMBIC") == "1":
        pytest.skip("SKIP_MYSQL_ALEMBIC=1")
    ok, s, exc = _mysql_ready()
    if not ok:
        pytest.skip(f"MySQL unavailable: {exc}")

    from sqlalchemy import create_engine, text

    engine = create_engine(s.database_url)
    with engine.begin() as c:
        tables = {r[0] for r in c.execute(text("SHOW TABLES")).fetchall()}
        if "orders" not in tables:
            pytest.skip("orders missing; run upgrade head first")
        c.execute(text("DELETE FROM orders WHERE order_id = 'ALEMBIC_KEEP_ME'"))
        c.execute(
            text(
                "INSERT INTO orders (order_id, user_name, payment_amount, order_status, event_version) "
                "VALUES ('ALEMBIC_KEEP_ME', 'probe', 1.00, 'ACTIVE', 0)"
            )
        )
        before = c.execute(
            text("SELECT COUNT(*) FROM orders WHERE order_id='ALEMBIC_KEEP_ME'")
        ).scalar()
        assert before == 1, "probe insert failed"
    engine.dispose()

    _alembic("upgrade", "head")

    engine = create_engine(s.database_url)
    with engine.begin() as c:
        after = c.execute(
            text("SELECT COUNT(*) FROM orders WHERE order_id='ALEMBIC_KEEP_ME'")
        ).scalar()
        assert after == 1, "upgrade head must not delete existing rows"
        c.execute(text("DELETE FROM orders WHERE order_id = 'ALEMBIC_KEEP_ME'"))
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert ver == "0001_baseline"
    engine.dispose()


def test_empty_database_upgrade_head_on_mysql_if_available(monkeypatch):
    """Dedicated empty database -> upgrade head."""
    if os.environ.get("SKIP_MYSQL_ALEMBIC") == "1":
        pytest.skip("SKIP_MYSQL_ALEMBIC=1")
    ok, s, exc = _mysql_ready()
    if not ok:
        pytest.skip(f"MySQL unavailable: {exc}")

    import pymysql
    from sqlalchemy import create_engine, text

    empty_db = "ai_commerce_alembic_empty_check"
    conn = pymysql.connect(host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password)
    with conn.cursor() as c:
        c.execute(f"DROP DATABASE IF EXISTS `{empty_db}`")
        c.execute(
            f"CREATE DATABASE `{empty_db}` DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_NAME", empty_db)
    monkeypatch.setenv("DB_HOST", str(s.db_host))
    monkeypatch.setenv("DB_PORT", str(s.db_port))
    monkeypatch.setenv("DB_USER", s.db_user)
    monkeypatch.setenv("DB_PASSWORD", s.db_password)
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("JWT_SECRET", "ci-test-secret-not-for-production")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin123")
    from backend import config as backend_config

    backend_config.get_settings.cache_clear()
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(_ALEMBIC_INI), "upgrade", "head"],
            cwd=str(_ALEMBIC_CWD),
            check=True,
            env={
                **os.environ,
                "DB_NAME": empty_db,
                "PYTHONPATH": os.pathsep.join(
                    filter(None, [str(_ROOT), os.environ.get("PYTHONPATH", "")])
                ),
            },
        )
        engine = create_engine(
            f"mysql+pymysql://{s.db_user}:{s.db_password}@{s.db_host}:{s.db_port}/{empty_db}"
            f"?charset=utf8mb4"
        )
        with engine.connect() as c:
            tables = {r[0] for r in c.execute(text("SHOW TABLES")).fetchall()}
            assert {"orders", "order_events", "app_metadata", "alembic_version"} <= tables
            assert (
                c.execute(text("SELECT version_num FROM alembic_version")).scalar()
                == "0001_baseline"
            )
            assert c.execute(text("SELECT COUNT(*) FROM orders")).scalar() == 0
        engine.dispose()
    finally:
        backend_config.get_settings.cache_clear()
        conn = pymysql.connect(
            host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password
        )
        with conn.cursor() as c:
            c.execute(f"DROP DATABASE IF EXISTS `{empty_db}`")
        conn.commit()
        conn.close()
