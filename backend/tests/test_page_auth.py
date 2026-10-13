"""页面认证契约测试（ASGI，不启动 Nginx）。"""
from datetime import timedelta

import httpx
import pytest

from backend.main import app
from backend.utils.auth import create_access_token


@pytest.mark.asyncio
async def test_page_rejects_anonymous_forged_and_expired_tokens():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for token in ("", "forged", create_access_token("admin", timedelta(seconds=-1))):
            response = await client.get("/api/auth/page-session", headers={"Cookie": f"ea_session={token}"})
            assert response.status_code == 401
        assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_login_sets_http_only_cookie_and_api_still_requires_bearer():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert (await client.get("/api/auth/page-session")).json()["username"] == "admin"
        assert (await client.get("/api/auth/me")).status_code == 401


def test_direct_page_gate_fails_closed_and_does_not_send_identity(monkeypatch):
    from agent_core.page_auth import verify_page_session

    with pytest.raises(ValueError):
        verify_page_session("")
    monkeypatch.setenv("AUTH_BACKEND_URL", "http://127.0.0.1:1")
    with pytest.raises(httpx.HTTPError):
        verify_page_session("forged")
