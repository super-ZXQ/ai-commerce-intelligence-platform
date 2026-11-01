"""批次1回归：数据保留、导入审计、状态对账与模型地址策略。"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote_plus

import pytest
from sqlalchemy import create_engine, text

from agent_core.provider_policy import (
    ProviderPolicyError,
    build_byok_config,
    validate_model_name,
    validate_provider_url,
)
from agent_core.model_adapter import OpenAIModelAdapter
from backend.scripts import import_orders as import_module
from backend.scripts.import_orders import (
    CsvValidationError,
    ImportGuardError,
    REPLACE_CONFIRMATION,
    run_import,
    scan_csv,
)
from backend.scripts.reconcile_orders import APPLY_CONFIRMATION, run_reconcile

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
_ALEMBIC_INI = _BACKEND / "alembic.ini"

_HEADER = [
    "订单顺序编号",
    "订单号",
    "用户名",
    "商品编号",
    "订单金额",
    "付款金额",
    "渠道编号",
    "平台类型",
    "下单时间",
    "付款时间",
    "是否退款",
    "优惠金额",
    "支付耗时_秒",
    "下单日期",
    "下单小时",
    "星期几",
]


def _row(seq: int, order_id: str, *, refund: str = "否", payment: str = "10.00") -> list[str]:
    return [
        str(seq),
        order_id,
        f"user-{seq}",
        f"PR{seq:06d}",
        payment,
        payment,
        "渠道1",
        "APP",
        "2025-01-01 10:00:00",
        "2025-01-01 10:00:10",
        refund,
        "0.00",
        "10.0",
        "2025-01-01",
        "10",
        "Wednesday",
    ]


def _write_csv(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(_HEADER)
        writer.writerows(rows)
    return path


def _query(url: str, sql: str, params: dict | None = None):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params or {}).fetchall()
    finally:
        engine.dispose()


def _execute(url: str, sql: str, params: dict | None = None) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params or {})
    finally:
        engine.dispose()


def _mysql_settings():
    try:
        import pymysql

        from backend.config import get_settings

        settings = get_settings()
        conn = pymysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
        )
        conn.close()
        return True, settings
    except Exception as exc:  # noqa: BLE001
        return False, exc


@pytest.fixture
def import_db():
    """每次测试创建独立数据库并升级到 Alembic head，结束后删除。"""
    ok, settings_or_exc = _mysql_settings()
    if not ok:
        pytest.skip(f"MySQL unavailable: {settings_or_exc}")
    settings = settings_or_exc
    import pymysql
    from uuid import uuid4

    db_name = f"ea_batch1_{uuid4().hex[:10]}"
    root = pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        autocommit=True,
    )
    with root.cursor() as cursor:
        cursor.execute(
            f"CREATE DATABASE `{db_name}` DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    root.close()

    env = {
        **os.environ,
        "DB_NAME": db_name,
        "DB_HOST": str(settings.db_host),
        "DB_PORT": str(settings.db_port),
        "DB_USER": settings.db_user,
        "DB_PASSWORD": settings.db_password,
        "DEBUG": "true",
        "JWT_SECRET": "pytest-batch1-secret",
        "ADMIN_PASSWORD": "admin123",
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(_ROOT), os.environ.get("PYTHONPATH", "")])
        ),
    }
    env.pop("MIGRATION_DB_USER", None)
    env.pop("MIGRATION_DB_PASSWORD", None)
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(_ALEMBIC_INI), "upgrade", "head"],
            cwd=str(_ROOT),
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        url = (
            f"mysql+pymysql://{quote_plus(settings.db_user)}:"
            f"{quote_plus(settings.db_password)}@{settings.db_host}:{settings.db_port}/"
            f"{db_name}?charset=utf8mb4"
        )
        yield url
    finally:
        root = pymysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            autocommit=True,
        )
        with root.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        root.close()


def test_scan_csv_handles_bom_and_reports_stats(tmp_path: Path):
    csv_path = _write_csv(
        tmp_path / "orders.csv",
        [
            _row(1, "T-001", refund="是", payment="10.00"),
            _row(2, "T-002", payment="20.00"),
        ],
    )
    stats = scan_csv(csv_path)

    assert stats.rows == 2
    assert stats.refund_rows == 1
    assert stats.payment_sum == 30
    assert stats.min_order_date.isoformat() == "2025-01-01"
    assert len(stats.sha256) == 64


@pytest.mark.parametrize(
    "rows",
    [
        [_row(1, "DUP"), _row(2, "DUP")],
        [_row(1, "BAD-REFUND", refund="maybe")],
        [_row(1, "NEG", refund="否", payment="-1")],
    ],
)
def test_scan_csv_rejects_invalid_rows(tmp_path: Path, rows):
    csv_path = _write_csv(tmp_path / "orders.csv", rows)
    with pytest.raises(CsvValidationError):
        scan_csv(csv_path)


def test_replace_requires_explicit_confirmation(tmp_path: Path):
    csv_path = _write_csv(tmp_path / "orders.csv", [_row(1, "T-001")])
    with pytest.raises(ImportGuardError, match="显式确认"):
        run_import(
            mode="replace",
            csv_path=csv_path,
            operator="pytest",
            database_url="mysql+pymysql://unused/unused",
        )


def test_bootstrap_import_then_guard_blocks_overwrite(tmp_path: Path, import_db):
    csv_path = _write_csv(
        tmp_path / "orders.csv",
        [_row(1, "T-001"), _row(2, "T-002"), _row(3, "T-003")],
    )
    result = run_import(
        mode="bootstrap",
        csv_path=csv_path,
        operator="pytest",
        database_url=import_db,
    )

    assert result["status"] == "completed"
    assert result["rows_imported"] == 3
    assert _query(import_db, "SELECT COUNT(*) FROM orders")[0][0] == 3
    assert {row[0] for row in _query(import_db, "SELECT DISTINCT order_status FROM orders")} == {
        "ACTIVE"
    }
    assert _query(import_db, "SELECT status FROM data_import_runs")[-1][0] == "completed"
    assert _query(import_db, "SELECT data_version FROM data_state WHERE id=1")[0][0] == 2

    with pytest.raises(ImportGuardError, match="bootstrap 仅允许空库"):
        run_import(
            mode="bootstrap",
            csv_path=csv_path,
            operator="pytest",
            database_url=import_db,
        )

    # 模拟运行期事件推进后重启/再次导入：必须拒绝全量替换，原数据保留。
    _execute(
        import_db,
        "UPDATE orders SET event_version=1 WHERE order_id='T-002'",
    )
    with pytest.raises(ImportGuardError, match="禁止全量替换"):
        run_import(
            mode="replace",
            csv_path=csv_path,
            operator="pytest",
            database_url=import_db,
            confirm=REPLACE_CONFIRMATION,
        )
    assert _query(import_db, "SELECT COUNT(*) FROM orders")[0][0] == 3


def test_replace_keeps_backup_and_updates_data_version(tmp_path: Path, import_db):
    first = _write_csv(tmp_path / "first.csv", [_row(1, "T-001"), _row(2, "T-002")])
    second = _write_csv(
        tmp_path / "second.csv",
        [_row(10, "T-010"), _row(11, "T-011"), _row(12, "T-012")],
    )
    run_import(mode="bootstrap", csv_path=first, operator="pytest", database_url=import_db)
    version_before = _query(import_db, "SELECT data_version FROM data_state WHERE id=1")[0][0]

    result = run_import(
        mode="replace",
        csv_path=second,
        operator="pytest",
        database_url=import_db,
        confirm=REPLACE_CONFIRMATION,
    )

    assert result["status"] == "completed"
    assert result["rows_imported"] == 3
    backup = result["backup_table"]
    assert _query(import_db, f"SELECT COUNT(*) FROM `{backup}`")[0][0] == 2
    assert _query(import_db, "SELECT COUNT(*) FROM orders")[0][0] == 3
    assert (
        _query(import_db, "SELECT data_version FROM data_state WHERE id=1")[0][0]
        == version_before + 1
    )
    assert _query(import_db, "SELECT status FROM data_import_runs ORDER BY id")[-1][0] == "completed"


def test_failed_staging_validation_keeps_original_orders(
    tmp_path: Path, import_db, monkeypatch
):
    first = _write_csv(tmp_path / "first.csv", [_row(1, "T-001"), _row(2, "T-002")])
    second = _write_csv(tmp_path / "second.csv", [_row(10, "T-010")])
    run_import(mode="bootstrap", csv_path=first, operator="pytest", database_url=import_db)

    def fail_validation(*_args, **_kwargs):
        raise CsvValidationError("simulated staging failure")

    monkeypatch.setattr(import_module, "_validate_staging", fail_validation)
    with pytest.raises(CsvValidationError, match="simulated"):
        run_import(
            mode="replace",
            csv_path=second,
            operator="pytest",
            database_url=import_db,
            confirm=REPLACE_CONFIRMATION,
        )

    assert _query(import_db, "SELECT COUNT(*) FROM orders")[0][0] == 2
    assert _query(import_db, "SELECT status FROM data_import_runs ORDER BY id")[-1][0] == "failed"
    staging = _query(
        import_db,
        "SELECT TABLE_NAME FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'orders_staging_%'",
    )
    assert staging == []


def test_reconcile_dry_run_and_apply_only_ledger_free_rows(tmp_path: Path, import_db):
    csv_path = _write_csv(
        tmp_path / "orders.csv",
        [
            _row(1, "T-001", refund="是"),
            _row(2, "T-002", refund="是"),
            _row(3, "T-003"),
            _row(4, "T-004"),
        ],
    )
    run_import(mode="bootstrap", csv_path=csv_path, operator="pytest", database_url=import_db)
    _execute(
        import_db,
        "INSERT INTO order_events "
        "(event_id, order_id, event_type, occurred_at, sequence, source, payload, processing_status) "
        "VALUES ('evt-1', 'T-002', 'ORDER_CREATED', NOW(), 1, 'pytest', '{}', 'APPLIED')",
    )

    dry = run_reconcile(
        apply=False, confirm=None, operator="pytest", database_url=import_db
    )
    assert dry["candidates"] == 1
    assert dry["updated"] == 0
    assert _query(import_db, "SELECT COUNT(*) FROM order_status_reconciliation")[0][0] == 0

    with pytest.raises(ValueError, match="显式确认"):
        run_reconcile(
            apply=True, confirm="wrong", operator="pytest", database_url=import_db
        )

    applied = run_reconcile(
        apply=True,
        confirm=APPLY_CONFIRMATION,
        operator="pytest",
        database_url=import_db,
    )
    assert applied["updated"] == 1
    statuses = {
        row[0]: row[1]
        for row in _query(import_db, "SELECT order_id, order_status FROM orders")
    }
    assert statuses == {
        "T-001": "REFUNDED",
        "T-002": "ACTIVE",
        "T-003": "ACTIVE",
        "T-004": "ACTIVE",
    }
    audit = [
        (row[0], row[1], row[2], row[3])
        for row in _query(
            import_db,
            "SELECT run_id, order_id, old_status, new_status FROM order_status_reconciliation",
        )
    ]
    assert audit == [(applied["run_id"], "T-001", "ACTIVE", "REFUNDED")]


def test_alembic_empty_database_keeps_orders_empty(import_db):
    assert _query(import_db, "SELECT COUNT(*) FROM orders")[0][0] == 0
    assert _query(import_db, "SELECT COUNT(*) FROM order_events")[0][0] == 0
    assert _query(import_db, "SELECT data_version FROM data_state WHERE id=1")[0][0] == 1


def test_provider_policy_rejects_unsafe_urls():
    with pytest.raises(ProviderPolicyError):
        validate_provider_url("http://api.deepseek.com", resolve_dns=False)
    with pytest.raises(ProviderPolicyError):
        validate_provider_url(
            "https://user:pass@api.deepseek.com", resolve_dns=False
        )
    with pytest.raises(ProviderPolicyError):
        validate_provider_url(
            "https://api.deepseek.com.evil.test",
            allowed_hosts=("api.deepseek.com",),
            resolve_dns=False,
        )
    with pytest.raises(ProviderPolicyError):
        validate_provider_url("https://127.0.0.1", resolve_dns=False)
    with pytest.raises(ProviderPolicyError):
        validate_provider_url("https://169.254.169.254", resolve_dns=False)


def test_provider_policy_checks_dns_results_and_allows_public_hosts():
    def private_resolver(*_args, **_kwargs):
        return [(2, 1, 6, "", ("10.0.0.7", 443))]

    def public_resolver(*_args, **_kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    with pytest.raises(ProviderPolicyError):
        validate_provider_url(
            "https://provider.example",
            allowed_hosts=("provider.example",),
            resolver=private_resolver,
        )
    assert (
        validate_provider_url(
            "https://provider.example/",
            allowed_hosts=("provider.example",),
            resolver=public_resolver,
        )
        == "https://provider.example"
    )


def test_provider_policy_byok_requires_allowlist_and_model_name_shape():
    assert validate_model_name("deepseek-chat") == "deepseek-chat"
    with pytest.raises(ProviderPolicyError):
        validate_model_name("bad model; DROP TABLE orders")
    with pytest.raises(ProviderPolicyError):
        build_byok_config(
            api_key="k",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            allowed_hosts=(),
            resolve_dns=False,
        )


def test_adapter_rejects_unsafe_config_without_network():
    with pytest.raises(ProviderPolicyError):
        OpenAIModelAdapter(
            api_key="not-a-real-key",
            base_url="http://api.deepseek.com",
            model="deepseek-chat",
            business_context="test",
            allowed_hosts=("api.deepseek.com",),
        )
    with pytest.raises(ProviderPolicyError):
        OpenAIModelAdapter(
            api_key="not-a-real-key",
            base_url="https://api.deepseek.com",
            model="bad model",
            business_context="test",
            allowed_hosts=("api.deepseek.com",),
        )


def test_no_startup_sync_and_no_platform_key_in_session_defaults():
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assistant = (_ROOT / "ai-ecommerce-assistant" / "app.py").read_text(encoding="utf-8")

    assert "sync_orders.py" not in dockerfile
    assert "SYNC_ORDERS_ON_STARTUP" not in compose
    assert "data-import" in compose
    assert "st.session_state.model_api_key = API_KEY" not in assistant
    assert "value=st.session_state.model_api_key" not in assistant
    # 模型地址与平台密钥只由后端持有；助手仅通过 BACKEND_API_URL 调用 FastAPI。
    assert "BACKEND_API_URL" in assistant
    assert "allowed_hosts=" not in assistant
    assert "ChatOpenAI" not in assistant
    assert "用户无需也不能在页面填写平台密钥" in assistant
