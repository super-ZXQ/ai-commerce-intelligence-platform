"""统一认证、权限边界与后端 AI 网关回归测试。

覆盖：
- 登录写入 HttpOnly Cookie，/api/auth/verify 支持 Cookie；
- 角色权限与账号停用、token_version 吊销；
- 服务凭据认证与权限交集；
- /api/ai/config 返回后端状态；NDJSON 流事件与审计只存哈希；
- 额度、单用户并发与排队超时；
- Nginx 对 /ai/、/BI/ 与 WebSocket 的统一鉴权配置；
- 认证库连接串、Redis 会话库与 Cookie 配置校验。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

from backend.config import Settings
from backend.database import auth_session_factory
from backend.main import app
from backend.models.database_models import AIQueryAudit, AuthUser, ServiceClient
from backend.models.schemas import AgentUsage, AIQueryResponse
from backend.scripts.manage_auth import _parse_scopes
from backend.services import ai_limits, ai_service
from backend.utils.auth import decode_token, hash_password, hash_service_key


@pytest.fixture
def event_loop():
    import asyncio

    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client: AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest_asyncio.fixture
async def temp_admin():
    """临时管理员，避免测试依赖开发库中已有账号的口令。"""
    username = "admin_" + uuid.uuid4().hex[:8]
    password = "admin-pass-123456"
    async with auth_session_factory() as session:
        session.add(
            AuthUser(
                username=username,
                password_hash=hash_password(password),
                role="admin",
                scopes="",
                is_active=True,
                token_version=1,
            )
        )
        await session.commit()
    try:
        yield {"username": username, "password": password}
    finally:
        async with auth_session_factory() as session:
            await session.execute(delete(AuthUser).where(AuthUser.username == username))
            await session.commit()


@pytest_asyncio.fixture
async def admin_client(client: AsyncClient, temp_admin: dict):
    token = await _login(client, temp_admin["username"], temp_admin["password"])
    client.headers["Authorization"] = f"Bearer {token}"
    yield client


@pytest_asyncio.fixture
async def temp_identity():
    """创建临时用户/服务凭据，测试结束后清理。"""
    suffix = uuid.uuid4().hex[:8]
    created_users = ["ingest_" + suffix, "viewer_" + suffix]
    created_clients = ["svc_" + suffix]
    async with auth_session_factory() as session:
        session.add_all(
            [
                AuthUser(
                    username=created_users[0],
                    password_hash=hash_password("ingest-pass-123"),
                    role="ingest",
                    scopes="",
                    is_active=True,
                    token_version=1,
                ),
                AuthUser(
                    username=created_users[1],
                    password_hash=hash_password("viewer-pass-123"),
                    role="viewer",
                    scopes="",
                    is_active=True,
                    token_version=1,
                ),
                ServiceClient(
                    name=created_clients[0],
                    key_prefix="ci-service-k",
                    key_hash=hash_service_key("ci-service-secret-123"),
                    scopes="data:read",
                    is_active=True,
                ),
            ]
        )
        await session.commit()
    try:
        yield {
            "ingest": created_users[0],
            "viewer": created_users[1],
            "service": created_clients[0],
            "service_key": "ci-service-secret-123",
        }
    finally:
        async with auth_session_factory() as session:
            await session.execute(
                delete(AuthUser).where(AuthUser.username.in_(created_users))
            )
            await session.execute(
                delete(ServiceClient).where(ServiceClient.name.in_(created_clients))
            )
            await session.commit()


class TestSessionCookie:
    @pytest.mark.asyncio
    async def test_login_sets_httponly_cookie_and_verify_accepts_it(
        self, client: AsyncClient, temp_admin: dict
    ):
        response = await client.post(
            "/api/auth/login",
            json={"username": temp_admin["username"], "password": temp_admin["password"]},
        )
        assert response.status_code == 200
        cookie = response.headers.get("set-cookie", "")
        assert "ea_session=" in cookie
        assert "HttpOnly" in cookie
        assert "Path=/" in cookie

        verify = await client.get("/api/auth/verify")
        assert verify.status_code == 200
        assert verify.json()["username"] == temp_admin["username"]
        assert verify.json()["authenticated"] is True

        token = response.json()["access_token"]
        token_data = decode_token(token)
        assert token_data is not None
        # JWT 携带角色默认权限与显式权限的并集，便于客户端展示与排查。
        assert {"data:read", "ai:query", "admin:manage"} <= set(token_data.scopes)

    @pytest.mark.asyncio
    async def test_verify_without_session_is_401(self, client: AsyncClient):
        response = await client.get("/api/auth/verify")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_clears_cookie(self, client: AsyncClient, temp_admin: dict):
        await client.post(
            "/api/auth/login",
            json={"username": temp_admin["username"], "password": temp_admin["password"]},
        )
        logout = await client.post("/api/auth/logout")
        assert logout.status_code == 200
        assert (await client.get("/api/auth/verify")).status_code == 401


class TestRoleAndAccountState:
    @pytest.mark.asyncio
    async def test_ingest_role_cannot_read_business_data_or_ai(
        self, client: AsyncClient, temp_identity: dict
    ):
        token = await _login(client, temp_identity["ingest"], "ingest-pass-123")
        headers = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/api/orders", headers=headers)).status_code == 403
        assert (await client.get("/api/ai/config", headers=headers)).status_code == 403

    @pytest.mark.asyncio
    async def test_inactive_account_is_rejected(self, client: AsyncClient, temp_identity: dict):
        token = await _login(client, temp_identity["viewer"], "viewer-pass-123")
        headers = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/api/orders", headers=headers)).status_code == 200

        async with auth_session_factory() as session:
            await session.execute(
                update(AuthUser)
                .where(AuthUser.username == temp_identity["viewer"])
                .values(is_active=False)
            )
            await session.commit()

        assert (await client.get("/api/orders", headers=headers)).status_code == 401

    @pytest.mark.asyncio
    async def test_token_version_revokes_old_tokens(self, client: AsyncClient, temp_identity: dict):
        token = await _login(client, temp_identity["viewer"], "viewer-pass-123")
        headers = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/api/orders", headers=headers)).status_code == 200

        async with auth_session_factory() as session:
            await session.execute(
                update(AuthUser)
                .where(AuthUser.username == temp_identity["viewer"])
                .values(token_version=AuthUser.token_version + 1)
            )
            await session.commit()

        response = await client.get("/api/orders", headers=headers)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_service_key_uses_its_own_scopes(self, client: AsyncClient, temp_identity: dict):
        headers = {"X-API-Key": temp_identity["service_key"]}
        assert (await client.get("/api/analytics/sales-overview", headers=headers)).status_code == 200
        assert (await client.get("/api/ai/config", headers=headers)).status_code == 403
        assert (
            await client.get("/api/orders", headers={"X-API-Key": "wrong-key"})
        ).status_code == 401


class TestBackendAIConfig:
    @pytest.mark.asyncio
    async def test_config_reports_backend_state(
        self, admin_client: AsyncClient, temp_admin: dict
    ):
        response = await admin_client.get("/api/ai/config")
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] in {"managed", "byok"}
        assert "rag" in data and "database" in data
        assert set(data["limits"]) == {
            "daily_request_limit",
            "max_inflight_per_user",
            "max_concurrency",
            "query_timeout_seconds",
        }
        assert data["principal"]["name"] == temp_admin["username"]
        assert "api_key" not in data


class TestAIStreamGateway:
    @pytest.mark.asyncio
    async def test_stream_emits_ndjson_events_and_audits_hash_only(
        self, admin_client: AsyncClient, temp_admin: dict, monkeypatch: pytest.MonkeyPatch
    ):
        query = "统计最近七天销售额"
        request_id = f"test-{uuid.uuid4().hex[:12]}"

        monkeypatch.setattr(ai_service, "resolve_model_config", lambda **_kwargs: None)

        async def fake_stream(query_text, thread_id, *, owner, config, on_step, on_token):
            assert owner == temp_admin["username"]
            on_step({"name": "route", "status": "success", "duration_ms": 1, "summary": "data"})
            on_token("部分")
            on_token("回答")
            return AIQueryResponse(
                answer="最终回答",
                request_id=request_id,
                thread_id=thread_id,
                intent="data",
                sources=[],
                steps=[
                    {"name": "route", "status": "success", "duration_ms": 1, "summary": "data"}
                ],
                usage=AgentUsage(input_tokens=3, output_tokens=2, total_tokens=5, latency_ms=12),
            )

        monkeypatch.setattr(ai_service, "stream_natural_language_query", fake_stream)

        response = await admin_client.post(
            "/api/ai/stream", json={"query": query, "thread_id": "stream-test"}
        )
        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        types = [event["type"] for event in events]
        assert types[0] == "start"
        assert "step" in types and "token" in types
        assert types[-1] == "final"
        assert events[-1]["response"]["answer"] == "最终回答"
        assert "".join(e["delta"] for e in events if e["type"] == "token") == "部分回答"

        async with auth_session_factory() as session:
            row = (
                await session.execute(
                    select(AIQueryAudit).where(AIQueryAudit.request_id == request_id)
                )
            ).scalar_one()
            assert row.query_sha256 == hashlib.sha256(query.encode()).hexdigest()
            assert row.query_length == len(query)
            assert not hasattr(row, "query")
            await session.execute(delete(AIQueryAudit).where(AIQueryAudit.request_id == request_id))
            await session.commit()

    @pytest.mark.asyncio
    async def test_pop_last_turn_endpoint(
        self, admin_client: AsyncClient, temp_admin: dict, monkeypatch
    ):
        async def fake_pop(owner, thread_id, question=None):
            assert owner == temp_admin["username"]
            assert thread_id == "thread-1"
            assert question == "旧问题"
            return True

        monkeypatch.setattr(ai_service, "pop_last_conversation_turn", fake_pop)
        response = await admin_client.post(
            "/api/ai/pop-last-turn", json={"thread_id": "thread-1", "question": "旧问题"}
        )
        assert response.status_code == 200
        assert response.json() == {"removed": True}


class TestAILimits:
    @pytest.mark.asyncio
    async def test_daily_quota_exceeded(self, monkeypatch: pytest.MonkeyPatch):
        principal = f"quota-{uuid.uuid4().hex[:8]}"
        async with auth_session_factory() as session:
            session.add(
                AIQueryAudit(
                    request_id=f"r-{uuid.uuid4().hex[:8]}",
                    principal_name=principal,
                    thread_id="",
                    model_mode="managed",
                    intent="data",
                    outcome="success",
                    query_sha256="0" * 64,
                    query_length=1,
                    created_at=datetime.now(UTC),
                )
            )
            await session.commit()

        monkeypatch.setattr(
            ai_limits, "settings", SimpleNamespace(ai_daily_request_limit=1)
        )
        async with auth_session_factory() as session:
            with pytest.raises(ai_limits.AIDailyQuotaExceeded):
                await ai_limits.ensure_quota(session, principal)
            await session.execute(
                delete(AIQueryAudit).where(AIQueryAudit.principal_name == principal)
            )
            await session.commit()

    @pytest.mark.asyncio
    async def test_inflight_and_queue_timeout(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            ai_limits,
            "settings",
            SimpleNamespace(
                ai_daily_request_limit=100,
                ai_max_concurrency=1,
                ai_max_inflight_per_user=1,
                ai_queue_timeout_seconds=0.05,
            ),
        )
        ai_limits._semaphores.clear()
        ai_limits._user_inflight.clear()

        class FakeDB:
            async def scalar(self, _stmt):
                return 0

        try:
            first = await ai_limits.acquire_ai_slot("alice", FakeDB())
            with pytest.raises(ai_limits.AIInflightExceeded):
                await ai_limits.acquire_ai_slot("alice", FakeDB())

            # bob 没有超单用户并发，但全局并发已被 alice 占满，排队超时。
            with pytest.raises(ai_limits.AIQueueTimeout):
                await ai_limits.acquire_ai_slot("bob", FakeDB())
            first.release()
        finally:
            ai_limits._semaphores.clear()
            ai_limits._user_inflight.clear()


class TestNginxAuthClosure:
    def test_ai_and_bi_require_auth_request_including_websocket(self):
        source = (
            __import__("pathlib").Path(__file__).resolve().parents[2]
            / "deploy"
            / "nginx.conf"
        ).read_text(encoding="utf-8")
        assert "location = /_auth" in source
        for block_name in ("location /ai/ {", "location /BI/ {"):
            block = source.split(block_name, 1)[1].split("\n        }", 1)[0]
            assert "auth_request /_auth;" in block
            assert "Upgrade $http_upgrade" in block
            assert "error_page 401 = @login_redirect;" in block
        stream_block = source.split("location = /api/ai/stream {", 1)[1].split("\n        }", 1)[0]
        assert "proxy_buffering off;" in stream_block
        login_block = source.split("location = /login {", 1)[1].split("\n        }", 1)[0]
        assert "auth_request" not in login_block


class TestManageAuthScopes:
    def test_default_role_scopes_are_not_split_into_characters(self):
        # 回归：role_default_scopes 返回逗号字符串，直接 sorted/join 会把权限拆成字符。
        scopes = _parse_scopes(None, "analyst")
        assert scopes == {"data:read", "ai:query"}

    def test_unknown_explicit_scope_is_rejected(self):
        with pytest.raises(SystemExit, match="未知权限"):
            _parse_scopes("data:read,root:everything", "analyst")


class TestAuthConfigValidation:
    def _settings(self, **overrides) -> Settings:
        values = dict(
            debug=True,
            jwt_secret="test-secret-not-for-production-use-only",
            admin_password="admin123",
            db_user="ea_app",
            db_password="app-p@ss",
            db_name="ea_test",
        )
        values.update(overrides)
        return Settings(**values)

    def test_auth_database_url_uses_least_privilege_account(self):
        settings = self._settings(auth_db_user="ea_auth", auth_db_password="auth:pw")
        assert "ea_auth" in settings.auth_async_database_url
        assert "auth%3Apw" in settings.auth_async_database_url
        assert "ea_app" not in settings.auth_async_database_url

    def test_auth_db_user_requires_password(self):
        with pytest.raises(ValueError, match="AUTH_DB_PASSWORD"):
            self._settings(auth_db_user="ea_auth")

    def test_session_redis_db_must_differ_from_cache_db(self):
        with pytest.raises(ValueError, match="REDIS_SESSION_DB"):
            self._settings(redis_db=0, redis_session_db=0)

    def test_session_redis_url_uses_session_db(self):
        settings = self._settings(redis_db=0, redis_session_db=2, redis_password="r:pass")
        assert settings.redis_session_url.endswith("/2")
        assert settings.redis_url.endswith("/0")

    def test_cookie_samesite_none_requires_secure(self):
        with pytest.raises(ValueError, match="AUTH_COOKIE_SECURE"):
            self._settings(auth_cookie_samesite="none", auth_cookie_secure=False)

    def test_cookie_name_is_validated(self):
        with pytest.raises(ValueError, match="AUTH_COOKIE_NAME"):
            self._settings(auth_cookie_name="bad cookie;name")

    def test_ai_limit_values_must_be_positive(self):
        with pytest.raises(ValueError, match="AI_MAX_CONCURRENCY"):
            self._settings(ai_max_concurrency=0)

    def test_production_rejects_truncated_bcrypt_hash(self):
        # Docker Compose 未转义 $ 时会把哈希吞成 "$2b$12$"，必须在启动时拒绝。
        with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
            Settings(
                _env_file=None,
                debug=False,
                jwt_secret="a" * 40,
                admin_password="$2b$12$",
            )
