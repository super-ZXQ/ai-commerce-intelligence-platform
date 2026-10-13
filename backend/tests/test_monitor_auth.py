"""监控端点访问保护开关测试。

覆盖两种状态：
1. 默认关闭（MONITOR_AUTH_REQUIRED=false）：免令牌可访问，保持向后兼容；
2. 开启后：无令牌 401，无效令牌 401，有效 JWT 正常返回。
"""
from httpx import ASGITransport, AsyncClient

from backend.config import get_settings
from backend.main import app

# 需要保护的监控端点（含 /metrics、/health/detailed 两个公开别名）。
_MONITOR_PATHS = [
    "/api/monitor/metrics",
    "/api/monitor/health/detailed",
    "/api/monitor/services-status",
    "/api/monitor/rag-stats",
    "/api/monitor/rag-stats.prom",
    "/metrics",
    "/health/detailed",
]


async def _login(ac: AsyncClient) -> str:
    r = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return r.json()["access_token"]


class TestMonitorAuthDisabled:
    """默认关闭：所有监控端点免鉴权可访问（向后兼容）。"""

    async def test_all_paths_open_without_token(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            for path in _MONITOR_PATHS:
                r = await ac.get(path)
                assert r.status_code == 200, f"{path} 默认关闭时应 200，实际 {r.status_code}"


class TestMonitorAuthEnabled:
    """开启后：全部端点要求 JWT。"""

    async def _enable(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "monitor_auth_required", True)

    async def test_all_paths_reject_without_token(self, monkeypatch):
        await self._enable(monkeypatch)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            for path in _MONITOR_PATHS:
                r = await ac.get(path)
                assert r.status_code == 401, f"{path} 开启后无令牌应 401，实际 {r.status_code}"

    async def test_reject_invalid_token(self, monkeypatch):
        await self._enable(monkeypatch)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get(
                "/api/monitor/metrics",
                headers={"Authorization": "Bearer not-a-real-token"},
            )
            assert r.status_code == 401

    async def test_allow_valid_token(self, monkeypatch):
        await self._enable(monkeypatch)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            token = await _login(ac)
            for path in _MONITOR_PATHS:
                r = await ac.get(path, headers={"Authorization": f"Bearer {token}"})
                assert r.status_code == 200, f"{path} 带有效令牌应 200，实际 {r.status_code}"
