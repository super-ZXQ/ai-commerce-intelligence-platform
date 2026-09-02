import asyncio
from decimal import Decimal
from inspect import Signature, signature
from types import SimpleNamespace

import pytest

from backend.config import Settings
from backend.models.schemas import SalesOverviewResponse
from backend.scripts.sync_orders import build_import_sql
from backend.services import ai_service, rfm_service
from backend.services.rfm_service import (
    _score_users,
    _summary_from_snapshot,
    get_rfm_top_users,
)
from backend.utils import cache
from backend.utils.rate_limiter import _get_client_id
from backend.utils.rfm_scoring import assign_segment, quantile_scores
from backend.utils.sql_guard import is_read_only_sql


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "WITH totals AS (SELECT 1 AS n) SELECT n FROM totals",
    "SHOW TABLES",
    "DESCRIBE orders",
    "SELECT ';' AS value",
    "SELECT '-- not a comment' AS value",
])
def test_read_only_sql_accepts_queries(sql):
    assert is_read_only_sql(sql)


@pytest.mark.parametrize("sql", [
    "DELETE\nFROM orders",
    "WITH x AS (SELECT 1) DELETE FROM orders",
    "SELECT * FROM orders INTO OUTFILE '/tmp/orders.csv'",
    "CALL dangerous_proc()",
    "SET @x = 1",
    "SELECT 1; DROP TABLE orders",
    "SELECT '--'; DROP TABLE orders",
    "SELECT '/*'; DELETE FROM orders",
    "SELECT SLEEP(10)",
])
def test_read_only_sql_blocks_mutation_and_side_effects(sql):
    assert not is_read_only_sql(sql)


def test_rfm_recent_user_gets_lower_r_score():
    users = [
        {"user_name": "recent", "recency_days": 1, "frequency": 3, "monetary": 3000},
        {"user_name": "middle", "recency_days": 30, "frequency": 2, "monetary": 1000},
        {"user_name": "old", "recency_days": 180, "frequency": 1, "monetary": 100},
    ]
    scored = _score_users(users, n_bins=3)
    by_name = {u["user_name"]: u for u in scored}
    assert by_name["recent"]["r_score"] < by_name["old"]["r_score"]
    assert by_name["recent"]["segment"].startswith("重要")
    assert by_name["old"]["segment"].startswith("一般")


def test_quantile_scores_handle_duplicate_boundaries():
    values = [1] * 80 + [2] * 15 + [3] * 5
    scores = quantile_scores(values, n_bins=5)

    assert set(scores[:80]) == {1}
    assert set(scores[80:95]) == {5}
    assert set(scores[95:]) == {5}


def test_shared_segment_semantics():
    assert assign_segment(1, 5, 5, 3, 3, 3) == "重要价值客户"
    assert assign_segment(5, 1, 1, 3, 3, 3) == "一般挽留客户"


def test_rfm_redis_summary_excludes_full_user_lists():
    snapshot = {
        "reference_date": "2024-01-01",
        "total_users": 1,
        "n_bins": 5,
        "score_threshold": 3,
        "averages": {"recency_days": 1, "frequency": 2, "monetary": 100},
        "segments": [],
        "users": [{"user_name": "u1"}],
        "sorted_users": [{"user_name": "u1"}],
    }

    summary = _summary_from_snapshot(snapshot)

    assert "users" not in summary
    assert "sorted_users" not in summary
    assert summary["total_users"] == 1


def test_ai_query_uses_database_column_aliases(monkeypatch):
    class FakeMappings:
        @staticmethod
        def all():
            return [{
                "refund_rate": Decimal("13.18"),
                "avg_order_value": Decimal("994.81"),
            }]

    class FakeResult:
        @staticmethod
        def mappings():
            return FakeMappings()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def execute(_statement):
            return FakeResult()

    class FakeEngine:
        @staticmethod
        def connect():
            return FakeConnection()

    monkeypatch.setattr(
        ai_service,
        "_get_sync_db",
        lambda: SimpleNamespace(_engine=FakeEngine()),
    )
    result = ai_service._execute_query_with_columns(
        "SELECT ROUND(1, 2) AS refund_rate, ROUND(3, 2) AS avg_order_value"
    )

    assert result == [{"refund_rate": 13.18, "avg_order_value": 994.81}]


@pytest.mark.asyncio
async def test_rfm_snapshot_prevents_concurrent_recalculation(monkeypatch):
    rfm_service.clear_rfm_snapshot_cache()
    calls = 0

    async def fake_build(_db, reference_date=None, n_bins=5):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"reference_date": "2024-01-01", "total_users": 0}

    monkeypatch.setattr(rfm_service, "_build_rfm_snapshot", fake_build)
    results = await asyncio.gather(
        *[rfm_service._get_rfm_snapshot(object()) for _ in range(5)]
    )

    assert calls == 1
    assert len(results) == 5
    rfm_service.clear_rfm_snapshot_cache()


@pytest.mark.asyncio
async def test_rfm_top_users_honors_requested_limit(monkeypatch):
    users = [{"user_name": f"u{i}"} for i in range(100)]

    async def fake_snapshot(_db, reference_date=None, n_bins=5):
        return {
            "reference_date": "2024-01-01",
            "total_users": 100,
            "sorted_users": users,
        }

    monkeypatch.setattr(rfm_service, "_get_rfm_snapshot", fake_snapshot)
    result = await get_rfm_top_users.__wrapped__(object(), limit=100)

    assert len(result["top_users"]) == 100


class _FakeRedis:
    def __init__(self):
        self.data = {}

    def setex(self, key, _ttl, value):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)


@pytest.mark.asyncio
async def test_cached_pydantic_model_round_trips_through_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "_redis_client", fake)
    monkeypatch.setattr(cache, "_redis_available", True)

    calls = 0

    @cache.cached(ttl=60)
    async def get_overview() -> SalesOverviewResponse:
        nonlocal calls
        calls += 1
        return SalesOverviewResponse(
            total_sales=100,
            total_orders=2,
            avg_order_value=50,
            total_users=2,
            refund_rate=0,
            total_refund_amount=0,
        )

    first = await get_overview()
    second = await get_overview()

    assert calls == 1
    assert isinstance(first, SalesOverviewResponse)
    assert isinstance(second, SalesOverviewResponse)
    assert second.total_sales == 100


@pytest.mark.asyncio
async def test_cached_function_without_return_annotation(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "_redis_client", fake)
    monkeypatch.setattr(cache, "_redis_available", True)

    calls = 0

    @cache.cached(ttl=60)
    async def get_data():
        nonlocal calls
        calls += 1
        return {"ok": True}

    assert signature(get_data).return_annotation is Signature.empty
    assert await get_data() == {"ok": True}
    assert await get_data() == {"ok": True}
    assert calls == 1


@pytest.mark.asyncio
async def test_cache_cleanup_removes_idle_redis_locks(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "_redis_client", fake)
    monkeypatch.setattr(cache, "_redis_available", True)
    cache._memory_cache.clear()
    cache._cache_locks.clear()

    @cache.cached(ttl=60)
    async def get_data():
        return {"ok": True}

    await get_data()
    assert len(cache._cache_locks) == 1
    assert cache.cleanup_memory_cache() == 0
    assert not cache._cache_locks


def test_proxy_headers_are_ignored_by_default():
    request = SimpleNamespace(
        headers={"x-real-ip": "203.0.113.9"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    assert _get_client_id(request) == "127.0.0.1"
    assert _get_client_id(request, trust_proxy_headers=True) == "203.0.113.9"


@pytest.mark.parametrize(
    ("jwt_secret", "admin_password"),
    [
        ("change-this-secret-in-production", "a-secure-admin-password"),
        ("short", "a-secure-admin-password"),
        ("a" * 32, "admin123"),
    ],
)
def test_production_rejects_insecure_credentials(jwt_secret, admin_password):
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            debug=False,
            jwt_secret=jwt_secret,
            admin_password=admin_password,
        )


def test_order_sync_targets_staging_table():
    sql = (
        "LOAD DATA INFILE '/var/lib/mysql-files/cleaned_orders.csv' "
        "INTO TABLE orders"
    )
    result = build_import_sql(sql, "orders_staging")
    assert "INTO TABLE orders_staging" in result
    assert "INTO TABLE orders " not in result
