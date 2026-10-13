"""事件与进行中缓存重建的竞争，防止失效后旧计算重新污染。"""
import asyncio

from backend.services import rfm_service
from backend.utils import cache


async def test_event_during_cache_rebuild_uses_new_generation(monkeypatch):
    monkeypatch.setattr(cache, "_redis_available", False)
    await cache.clear()
    entered, release = asyncio.Event(), asyncio.Event()
    value = 1

    @cache.cached(tags=("analytics",))
    async def read():
        captured = value
        entered.set()
        await release.wait()
        return captured

    old = asyncio.create_task(read())
    await entered.wait()
    value = 2
    await cache.invalidate_tags("analytics")
    release.set()
    assert await old == 1
    assert await read() == 2


async def test_event_during_rfm_rebuild_preserves_lock_and_invalidates_snapshot(monkeypatch):
    rfm_service.clear_rfm_snapshot_cache()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def build(*a, **k):
        nonlocal calls
        calls += 1
        captured = calls
        entered.set()
        await release.wait()
        return {"generation": captured}

    monkeypatch.setattr(rfm_service, "_build_rfm_snapshot", build)
    old = asyncio.create_task(rfm_service._get_rfm_snapshot(object()))
    await entered.wait()
    existing_lock = rfm_service._snapshot_locks[(None, 5)]
    rfm_service.clear_rfm_snapshot_cache()
    assert rfm_service._snapshot_locks[(None, 5)] is existing_lock
    release.set()
    assert (await old)["generation"] == 1
    assert (await rfm_service._get_rfm_snapshot(object()))["generation"] == 2
    rfm_service.clear_rfm_snapshot_cache()
