import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.database import event_session_factory
from backend.main import app
from backend.models.schemas import OrderEventInput
from backend.services import order_event_service


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
async def authed_client():
    """带 JWT 鉴权的 client fixture。

    统一在此登录 admin/admin123，注入 Authorization 头。
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        token = r.json()["access_token"]
        ac.headers["Authorization"] = f"Bearer {token}"
        yield ac


@pytest_asyncio.fixture
async def auth_token(client: AsyncClient):
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
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
    async def test_login_success(self, client: AsyncClient):
        r = await client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, client: AsyncClient):
        r = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
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
    async def test_me_with_token(self, client: AsyncClient, auth_token: str):
        r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {auth_token}"})
        assert r.status_code == 200
        assert r.json()["username"] == "admin"

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
    async def test_event_create_duplicate_refund_and_metrics_are_consistent(self, authed_client: AsyncClient):
        order_id = f"EVT-{uuid4().hex[:20]}"
        before = (await authed_client.get("/api/analytics/sales-overview")).json()["total_sales"]
        created = _created_event(order_id)

        first = await authed_client.post("/api/order-events", json=created)
        assert first.status_code == 200
        assert first.json()["results"][0]["status"] == "processed"
        after_create = (await authed_client.get("/api/analytics/sales-overview")).json()["total_sales"]
        assert after_create == pytest.approx(before + 100.0)

        duplicate = await authed_client.post("/api/order-events", json=created)
        assert duplicate.status_code == 200
        assert duplicate.json()["results"][0]["status"] == "duplicate"
        assert (await authed_client.get("/api/analytics/sales-overview")).json()["total_sales"] == pytest.approx(after_create)

        refunded = {
            "event_id": str(uuid4()), "order_id": order_id, "event_type": "ORDER_REFUNDED",
            "occurred_at": datetime.now(UTC).isoformat(), "version": 2, "source": "pytest", "payload": {},
        }
        response = await authed_client.post("/api/order-events", json=refunded)
        assert response.status_code == 200
        assert response.json()["results"][0]["status"] == "processed"
        after_refund = (await authed_client.get("/api/analytics/sales-overview")).json()["total_sales"]
        assert after_refund == pytest.approx(before)

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
    async def test_export_orders_csv_stream(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/export/orders?export_format=csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert r.content.startswith("\ufeff订单编号".encode("utf-8"))
        assert "attachment; filename=orders_export.csv" in r.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_export_orders_excel(self, authed_client: AsyncClient):
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
