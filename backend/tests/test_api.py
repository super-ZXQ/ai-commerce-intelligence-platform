import asyncio
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.main import app


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_token(client: AsyncClient):
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return r.json()["access_token"]


class TestSystem:
    @pytest.mark.asyncio
    async def test_root(self, client: AsyncClient):
        r = await client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert "name" in data
        assert "version" in data

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
    async def test_list_orders(self, client: AsyncClient):
        r = await client.get("/api/orders?page_size=2")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "total_pages" in data
        assert "items" in data
        assert len(data["items"]) <= 2

    @pytest.mark.asyncio
    async def test_get_order_detail(self, client: AsyncClient):
        r = await client.get("/api/orders/1")
        assert r.status_code == 200
        assert "order_no" in r.json()

    @pytest.mark.asyncio
    async def test_order_not_found(self, client: AsyncClient):
        r = await client.get("/api/orders/999999999")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_filter_orders(self, client: AsyncClient):
        r = await client.get("/api/orders/filter?platform_type=APP&page_size=3")
        assert r.status_code == 200
        assert "total" in r.json()

    @pytest.mark.asyncio
    async def test_invalid_sort_field(self, client: AsyncClient):
        r = await client.get("/api/orders?sort_by=nonexistent")
        assert r.status_code == 400


class TestAnalytics:
    @pytest.mark.asyncio
    async def test_sales_overview(self, client: AsyncClient):
        r = await client.get("/api/analytics/sales-overview")
        assert r.status_code == 200
        d = r.json()
        assert d["total_sales"] > 0
        assert d["total_orders"] > 0
        assert d["refund_rate"] >= 0

    @pytest.mark.asyncio
    async def test_sales_trend_monthly(self, client: AsyncClient):
        r = await client.get("/api/analytics/sales-trend?granularity=month")
        assert r.status_code == 200
        assert len(r.json()["data"]) > 0

    @pytest.mark.asyncio
    async def test_top_products(self, client: AsyncClient):
        r = await client.get("/api/analytics/top-products?limit=5")
        assert r.status_code == 200
        products = r.json()
        assert len(products) <= 5
        if products:
            assert "product_id" in products[0]
            assert "rank" in products[0]

    @pytest.mark.asyncio
    async def test_user_behavior(self, client: AsyncClient):
        r = await client.get("/api/analytics/user-behavior")
        assert r.status_code == 200
        d = r.json()
        assert d["repeat_purchase_rate"] >= 0
        assert d["active_users_7d"] >= 0

    @pytest.mark.asyncio
    async def test_category_analysis(self, client: AsyncClient):
        r = await client.get("/api/analytics/category-analysis")
        assert r.status_code == 200
        cats = r.json()["categories"]
        assert len(cats) > 0


class TestProductsUsers:
    @pytest.mark.asyncio
    async def test_products(self, client: AsyncClient):
        r = await client.get("/api/products?limit=3")
        assert r.status_code == 200
        assert len(r.json()) <= 3

    @pytest.mark.asyncio
    async def test_users(self, client: AsyncClient):
        r = await client.get("/api/users?limit=3")
        assert r.status_code == 200
        assert len(r.json()) <= 3


class TestAI:
    @pytest.mark.asyncio
    async def test_ai_requires_auth(self, client: AsyncClient):
        r = await client.post("/api/ai/query", json={"query": "test"})
        assert r.status_code == 401


class TestExport:
    @pytest.mark.asyncio
    async def test_export_analytics_csv(self, client: AsyncClient):
        r = await client.get("/api/export/analytics?format=csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_export_analytics_excel(self, client: AsyncClient):
        r = await client.get("/api/export/analytics?format=excel")
        assert r.status_code == 200
        assert "sheet" in r.headers.get("content-type", "")


class TestMonitor:
    @pytest.mark.asyncio
    async def test_metrics(self, client: AsyncClient):
        r = await client.get("/api/monitor/metrics")
        assert r.status_code == 200
        d = r.json()
        assert "server" in d
        assert "requests" in d

    @pytest.mark.asyncio
    async def test_detailed_health(self, client: AsyncClient):
        r = await client.get("/api/monitor/health/detailed")
        assert r.status_code == 200
        assert "checks" in r.json()


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_rate_limit_headers_present(self, client: AsyncClient):
        r = await client.get("/api/products?limit=1")
        assert r.status_code == 200
        assert "X-RateLimit-Limit" in r.headers
        assert "X-RateLimit-Remaining" in r.headers
