import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pymysql
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from backend.config import get_settings
from backend.database import event_session_factory
from backend.main import app
from backend.models.schemas import OrderEventInput
from backend.services import order_event_service
from backend.utils.auth import hash_password, role_default_scopes

TEST_ADMIN_USERNAME = "ci_admin"
TEST_ADMIN_PASSWORD = "ci-admin-pass-123"


def _admin_connection():
    settings = get_settings()
    return pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.auth_db_user or settings.db_user,
        password=settings.auth_db_password or settings.db_password,
        database=settings.db_name,
        charset="utf8mb4",
        autocommit=True,
    )


@pytest.fixture(scope="module")
def test_admin():
    """独立的测试管理员，不改写开发库里真实 admin 账号的口令。"""
    conn = _admin_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM auth_users WHERE username=%s", (TEST_ADMIN_USERNAME,))
            cur.execute(
                "INSERT INTO auth_users "
                "(username, password_hash, role, scopes, is_active, token_version) "
                "VALUES (%s, %s, 'admin', %s, 1, 1)",
                (TEST_ADMIN_USERNAME, hash_password(TEST_ADMIN_PASSWORD), role_default_scopes("admin")),
            )
    finally:
        conn.close()
    yield {"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD}
    conn = _admin_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM auth_users WHERE username=%s", (TEST_ADMIN_USERNAME,))
    finally:
        conn.close()


# 每个测试使用独立事件循环，避免 aiomysql 连接跨 loop 复用。
@pytest.fixture
def event_loop():
    """每个测试创建独立事件循环。"""
    import asyncio
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def authed_client(test_admin: dict):
    """带 JWT 鉴权的 client fixture，登录模块级测试管理员。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/api/auth/login",
            json={"username": test_admin["username"], "password": test_admin["password"]},
        )
        token = r.json()["access_token"]
        ac.headers["Authorization"] = f"Bearer {token}"
        yield ac


@pytest_asyncio.fixture
async def auth_token(client: AsyncClient, test_admin: dict):
    r = await client.post(
        "/api/auth/login",
        json={"username": test_admin["username"], "password": test_admin["password"]},
    )
    return r.json()["access_token"]


class TestSystem:
    @pytest.mark.asyncio
    async def test_root(self, client: AsyncClient):
        # 根路径返回导航页 HTML。
        r = await client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_favicon_returns_real_icon(self, client: AsyncClient):
        # 图标必须返回真实内容：204 只是把 404 藏起来，浏览器仍然拿不到图标。
        for path in ("/favicon.ico", "/favicon.svg", "/static/favicon.svg"):
            r = await client.get(path)
            assert r.status_code == 200, f"{path} 返回 {r.status_code}，未返回真实图标"
            assert "image/svg+xml" in r.headers["content-type"]
            assert "<svg" in r.text, f"{path} 返回的不是 SVG 图标"

    @pytest.mark.asyncio
    async def test_health(self, client: AsyncClient):
        r = await client.get("/health")
        assert r.status_code in (200,)
        data = r.json()
        assert "status" in data
        assert "database" in data

    @pytest.mark.asyncio
    async def test_livez_does_not_touch_dependencies(self, client: AsyncClient, monkeypatch):
        """存活探针不能因数据库抖动而失败，否则编排器会误杀进程。"""
        async def _boom():
            raise AssertionError("/livez 不应访问数据库")

        monkeypatch.setattr("backend.main.check_db_connection", _boom)
        r = await client.get("/livez")
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}

    @pytest.mark.asyncio
    async def test_readyz_requires_all_three_databases(self, client: AsyncClient, monkeypatch):
        r = await client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"
        assert set(r.json()["checks"]) == {"database", "event_database", "auth_database"}

        async def _down():
            return False

        monkeypatch.setattr("backend.main.check_auth_db_connection", _down)
        r = await client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["status"] == "not_ready"
        assert r.json()["checks"]["auth_database"] is False

    @pytest.mark.asyncio
    async def test_openapi_schema(self, client: AsyncClient):
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert "/api/auth/login" in schema["paths"]
        assert "/api/orders" in schema["paths"]
        assert "/api/analytics/sales-overview" in schema["paths"]

    @pytest.mark.asyncio
    async def test_public_monitor_aliases(self, client: AsyncClient):
        health = await client.get("/health/detailed")
        metrics = await client.get("/metrics")
        services = await client.get("/api/monitor/services-status")

        assert health.status_code == 200
        assert "checks" in health.json()
        assert metrics.status_code == 200
        assert metrics.headers["content-type"].startswith("text/plain")
        assert services.status_code == 200


class TestAuth:
    @pytest.mark.asyncio
    async def test_login_success(self, client: AsyncClient, test_admin: dict):
        r = await client.post(
            "/api/auth/login",
            json={"username": test_admin["username"], "password": test_admin["password"]},
        )
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, client: AsyncClient, test_admin: dict):
        r = await client.post(
            "/api/auth/login",
            json={"username": test_admin["username"], "password": "wrong"},
        )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_login_wrong_user(self, client: AsyncClient):
        r = await client.post("/api/auth/login", json={"username": "nobody", "password": "xxx"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_me_without_token(self, client: AsyncClient):
        r = await client.get("/api/auth/me")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_me_with_token(
        self, client: AsyncClient, auth_token: str, test_admin: dict
    ):
        r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {auth_token}"})
        assert r.status_code == 200
        assert r.json()["username"] == test_admin["username"]

    @pytest.mark.asyncio
    async def test_refresh_token(self, client: AsyncClient, auth_token: str):
        r = await client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {auth_token}"})
        assert r.status_code == 200
        assert "access_token" in r.json()


class TestOrders:
    @pytest.mark.asyncio
    async def test_list_orders(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/orders?page_size=2")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "total_pages" in data
        assert "items" in data
        assert len(data["items"]) <= 2

    @pytest.mark.asyncio
    async def test_get_order_detail(self, authed_client: AsyncClient):
        listing = await authed_client.get("/api/orders?page_size=1")
        items = listing.json()["items"]
        if not items:
            pytest.skip("测试数据库没有订单数据")
        r = await authed_client.get(f"/api/orders/{items[0]['id']}")
        assert r.status_code == 200
        assert "order_no" in r.json()

    @pytest.mark.asyncio
    async def test_order_not_found(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/orders/999999999")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_filter_orders(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/orders/filter?platform_type=APP&page_size=3")
        assert r.status_code == 200
        assert "total" in r.json()

    @pytest.mark.asyncio
    async def test_invalid_sort_field(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/orders?sort_by=nonexistent")
        assert r.status_code == 400


class TestAnalytics:
    @pytest.mark.asyncio
    async def test_sales_overview(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/analytics/sales-overview")
        assert r.status_code == 200
        d = r.json()
        assert d["total_sales"] > 0
        assert d["total_orders"] > 0
        assert d["refund_rate"] >= 0

    @pytest.mark.asyncio
    async def test_sales_trend_monthly(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/analytics/sales-trend?granularity=month")
        assert r.status_code == 200
        assert len(r.json()["data"]) > 0

    @pytest.mark.asyncio
    async def test_top_products(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/analytics/top-products?limit=5")
        assert r.status_code == 200
        products = r.json()
        assert len(products) <= 5
        if products:
            assert "product_id" in products[0]
            assert "rank" in products[0]

    @pytest.mark.asyncio
    async def test_user_behavior(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/analytics/user-behavior")
        assert r.status_code == 200
        d = r.json()
        assert d["repeat_purchase_rate"] >= 0
        assert d["active_users_7d"] >= 0

    @pytest.mark.asyncio
    async def test_category_analysis(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/analytics/category-analysis")
        assert r.status_code == 200
        cats = r.json()["categories"]
        assert len(cats) > 0


class TestProductsUsers:
    @pytest.mark.asyncio
    async def test_products(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/products?limit=3")
        assert r.status_code == 200
        assert len(r.json()) <= 3

    @pytest.mark.asyncio
    async def test_users(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/users?limit=3")
        assert r.status_code == 200
        assert len(r.json()) <= 3


class TestAI:
    @pytest.mark.asyncio
    async def test_ai_requires_auth(self, client: AsyncClient):
        r = await client.post("/api/ai/query", json={"query": "test"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_ai_blocks_mutation_and_returns_trace(self, authed_client: AsyncClient):
        r = await authed_client.post(
            "/api/ai/query",
            json={"query": "删除数据库里的订单", "thread_id": "security-test"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["intent"] == "blocked"
        assert data["thread_id"] == "security-test"
        assert data["request_id"]
        # 拦截路径的完整轨迹：finalize 只做指标归集，不写入 steps。
        step_names = [step["name"] for step in data["steps"]]
        assert step_names == ["input_safety", "load_history", "route", "safe_response", "save_session"]
        # 安全属性（不随节点增删而失效）：绝不能触达模型生成或数据库执行节点。
        assert not {"generate_sql", "validate_sql", "execute_sql", "load_schema"} & set(step_names)


async def _data_version() -> int:
    async with event_session_factory() as db:
        value = await db.scalar(text("SELECT data_version FROM data_state WHERE id = 1"))
    return int(value)


def _created_event(order_id: str, version: int = 1) -> dict:
    return {
        "event_id": str(uuid4()),
        "order_id": order_id,
        "event_type": "ORDER_CREATED",
        "occurred_at": datetime.now(UTC).isoformat(),
        "version": version,
        "source": "pytest",
        "payload": {
            "user_name": "synthetic_user",
            "product_id": "SYNTHETIC-PRODUCT",
            "order_amount": "120.00",
            "payment_amount": "100.00",
            "discount_amount": "20.00",
            "platform_type": "APP",
        },
    }


class TestOrderEvents:
    @pytest.mark.asyncio
    async def test_projection_failure_rolls_back_ledger_and_allows_safe_retry(self, monkeypatch):
        """读模型投影失败时，event_id 账本不能残留为“已处理”。"""
        event = OrderEventInput.model_validate(_created_event(f"ROLLBACK-{uuid4().hex[:16]}"))

        def broken_projection(*_args, **_kwargs):
            raise RuntimeError("模拟订单读模型写入失败")

        monkeypatch.setattr(order_event_service, "_new_order", broken_projection)
        async with event_session_factory() as db:
            with pytest.raises(RuntimeError, match="模拟订单读模型写入失败"):
                await order_event_service.apply_order_event(db, event)

        monkeypatch.undo()
        async with event_session_factory() as db:
            retried = await order_event_service.apply_order_event(db, event)
        assert retried.status == "processed"

    @pytest.mark.asyncio
    async def test_event_create_duplicate_refund_cancel_and_metrics_are_consistent(
        self, authed_client: AsyncClient
    ):
        order_id = f"EVT-{uuid4().hex[:20]}"
        before = (await authed_client.get("/api/analytics/sales-overview")).json()
        created = _created_event(order_id)

        first = await authed_client.post("/api/order-events", json=created)
        assert first.status_code == 200
        assert first.json()["results"][0]["status"] == "processed"
        after_create = (await authed_client.get("/api/analytics/sales-overview")).json()
        assert after_create["total_sales"] == pytest.approx(before["total_sales"] + 100.0)
        assert after_create["total_orders"] == before["total_orders"] + 1

        duplicate = await authed_client.post("/api/order-events", json=created)
        assert duplicate.status_code == 200
        assert duplicate.json()["results"][0]["status"] == "duplicate"
        assert (
            await authed_client.get("/api/analytics/sales-overview")
        ).json()["total_sales"] == pytest.approx(after_create["total_sales"])

        refunded = {
            "event_id": str(uuid4()), "order_id": order_id, "event_type": "ORDER_REFUNDED",
            "occurred_at": datetime.now(UTC).isoformat(), "version": 2, "source": "pytest", "payload": {},
        }
        response = await authed_client.post("/api/order-events", json=refunded)
        assert response.status_code == 200
        assert response.json()["results"][0]["status"] == "processed"
        after_refund = (await authed_client.get("/api/analytics/sales-overview")).json()
        # 统一口径：退款订单保留在销售额与订单数中，退款金额单独累计。
        assert after_refund["total_sales"] == pytest.approx(after_create["total_sales"])
        assert after_refund["total_orders"] == after_create["total_orders"]
        assert after_refund["total_refund_amount"] == pytest.approx(
            before["total_refund_amount"] + 100.0
        )
        assert after_refund["refund_rate"] >= before["refund_rate"]

        cancel_id = f"CANCEL-{uuid4().hex[:20]}"
        cancelled = _created_event(cancel_id, version=1)
        assert (await authed_client.post("/api/order-events", json=cancelled)).json()["processed"] == 1
        cancel_event = {
            "event_id": str(uuid4()), "order_id": cancel_id, "event_type": "ORDER_CANCELLED",
            "occurred_at": datetime.now(UTC).isoformat(), "version": 2, "source": "pytest", "payload": {},
        }
        assert (await authed_client.post("/api/order-events", json=cancel_event)).json()["processed"] == 1
        after_cancel = (await authed_client.get("/api/analytics/sales-overview")).json()
        # 取消订单不计入任何经营指标，销售额与订单数回到取消前水平。
        assert after_cancel["total_sales"] == pytest.approx(after_refund["total_sales"])
        assert after_cancel["total_orders"] == after_refund["total_orders"]

    @pytest.mark.asyncio
    async def test_data_version_bumps_only_for_processed_events(self, authed_client: AsyncClient):
        order_id = f"VERSION-{uuid4().hex[:16]}"
        before = await _data_version()
        created = _created_event(order_id)
        assert (await authed_client.post("/api/order-events", json=created)).json()["processed"] == 1
        assert await _data_version() == before + 1

        duplicate = await authed_client.post("/api/order-events", json=created)
        assert duplicate.json()["results"][0]["status"] == "duplicate"
        assert await _data_version() == before + 1

        rejected = await authed_client.post(
            "/api/order-events", json=_created_event(order_id, version=1)
        )
        assert rejected.json()["results"][0]["status"] == "rejected"
        assert await _data_version() == before + 1

    @pytest.mark.asyncio
    async def test_concurrent_event_writes_keep_version_monotonic(self, authed_client: AsyncClient):
        before = await _data_version()
        events = [_created_event(f"VERSION-CONC-{uuid4().hex[:14]}") for _ in range(3)]
        responses = await asyncio.gather(*[
            authed_client.post("/api/order-events", json=event) for event in events
        ])
        assert all(response.status_code == 200 for response in responses)
        assert all(response.json()["results"][0]["status"] == "processed" for response in responses)
        # 全局版本行串行递增：并发事件全部提交后恰好增加 3。
        assert await _data_version() == before + 3

    @pytest.mark.asyncio
    async def test_concurrent_same_event_id_is_processed_once(self, authed_client: AsyncClient):
        event = _created_event(f"CONCURRENT-{uuid4().hex[:16]}")
        responses = await asyncio.gather(*[
            authed_client.post("/api/order-events", json=event) for _ in range(2)
        ])
        assert all(response.status_code == 200 for response in responses)
        statuses = sorted(response.json()["results"][0]["status"] for response in responses)
        assert statuses == ["duplicate", "processed"]

    @pytest.mark.asyncio
    async def test_out_of_order_and_terminal_transition_are_rejected(self, authed_client: AsyncClient):
        order_id = f"ORDERING-{uuid4().hex[:16]}"
        created = _created_event(order_id, version=10)
        assert (await authed_client.post("/api/order-events", json=created)).json()["processed"] == 1

        old_cancel = {
            "event_id": str(uuid4()), "order_id": order_id, "event_type": "ORDER_CANCELLED",
            "occurred_at": datetime.now(UTC).isoformat(), "version": 9, "source": "pytest", "payload": {},
        }
        rejected = await authed_client.post("/api/order-events", json=old_cancel)
        assert rejected.json()["results"][0]["status"] == "rejected"
        assert "版本" in rejected.json()["results"][0]["reason"]

        refund = {**old_cancel, "event_id": str(uuid4()), "event_type": "ORDER_REFUNDED", "version": 11}
        assert (await authed_client.post("/api/order-events", json=refund)).json()["processed"] == 1
        later_cancel = {**old_cancel, "event_id": str(uuid4()), "version": 12}
        terminal = await authed_client.post("/api/order-events", json=later_cancel)
        assert terminal.json()["results"][0]["status"] == "rejected"
        assert "不能转换" in terminal.json()["results"][0]["reason"]

    @pytest.mark.asyncio
    async def test_event_contract_rejects_unknown_type_invalid_amount_and_oversized_batch(self, authed_client: AsyncClient):
        unknown = _created_event("INVALID-TYPE")
        unknown["event_type"] = "ORDER_PAID"
        assert (await authed_client.post("/api/order-events", json=unknown)).status_code == 422

        invalid_amount = _created_event("INVALID-AMOUNT")
        invalid_amount["payload"]["payment_amount"] = "-1.00"
        assert (await authed_client.post("/api/order-events", json=invalid_amount)).status_code == 422

        batch = {"events": [_created_event(f"BATCH-{index}") for index in range(101)]}
        assert (await authed_client.post("/api/order-events", json=batch)).status_code == 422


class TestExport:
    @pytest.mark.asyncio
    async def test_export_orders_rejects_total_over_limit(self, authed_client: AsyncClient, monkeypatch):
        from types import SimpleNamespace

        from backend.routes import export as export_routes

        monkeypatch.setattr(
            export_routes,
            "settings",
            SimpleNamespace(export_max_rows=1, export_excel_max_rows=1),
        )
        r = await authed_client.get("/api/export/orders?export_format=csv")
        assert r.status_code == 413
        assert r.json()["detail"]["error_code"] == "EXPORT_TOO_LARGE"

    @pytest.mark.asyncio
    async def test_export_orders_excel_has_lower_limit(self, authed_client: AsyncClient, monkeypatch):
        from types import SimpleNamespace

        from backend.routes import export as export_routes

        monkeypatch.setattr(
            export_routes,
            "settings",
            SimpleNamespace(export_max_rows=10**9, export_excel_max_rows=1),
        )
        r = await authed_client.get("/api/export/orders?export_format=excel")
        assert r.status_code == 413
        assert r.json()["detail"]["error_code"] == "EXPORT_EXCEL_TOO_LARGE"

    @pytest.mark.asyncio
    async def test_export_orders_csv_stream(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/export/orders?export_format=csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert r.content.startswith("\ufeff订单编号".encode("utf-8"))
        assert "attachment; filename=orders_export.csv" in r.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_export_orders_excel(self, authed_client: AsyncClient, monkeypatch):
        from types import SimpleNamespace

        from backend.routes import export as export_routes

        # 默认 Excel 上限低于开发库全量行数；此处放开上限，仅验证工作簿生成。
        monkeypatch.setattr(
            export_routes,
            "settings",
            SimpleNamespace(export_max_rows=10**9, export_excel_max_rows=10**9),
        )
        r = await authed_client.get("/api/export/orders?export_format=excel")
        assert r.status_code == 200
        assert "spreadsheetml.sheet" in r.headers["content-type"]
        assert r.content.startswith(b"PK")
        assert "attachment; filename=orders_export.xlsx" in r.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_export_analytics_csv(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/export/analytics?format=csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_export_analytics_excel(self, authed_client: AsyncClient):
        # Excel 导出使用明确的 export_format 参数。
        r = await authed_client.get("/api/export/analytics?export_format=excel")
        assert r.status_code == 200
        assert "spreadsheet" in r.headers.get("content-type", "")


class TestMonitor:
    @pytest.mark.asyncio
    async def test_metrics(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/monitor/metrics")
        assert r.status_code == 200
        d = r.json()
        assert "server" in d
        assert "requests" in d

    @pytest.mark.asyncio
    async def test_detailed_health(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/monitor/health/detailed")
        assert r.status_code == 200
        assert "checks" in r.json()


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_rate_limit_headers_present(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/products?limit=1")
        assert r.status_code == 200
        assert "X-RateLimit-Limit" in r.headers
        assert "X-RateLimit-Remaining" in r.headers
