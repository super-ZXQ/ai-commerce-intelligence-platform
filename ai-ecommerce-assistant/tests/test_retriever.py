"""Retriever 测试：缓存 / TTL / LRU / 阈值 / 超时 / 格式化 / stats。

不依赖真实 VectorStore，全部用 Mock。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from rag.retriever import Retriever


@pytest.fixture
def fake_store() -> MagicMock:
    """Mock VectorStore.search，按 query 返回伪结果。"""
    s = MagicMock()
    s.knowledge_version = "v1"
    s.count.return_value = 1

    def _search(query, k=3, score_threshold=None, filter=None):
        return [{
            "content": f"doc for {query}",
            "metadata": {"source": "biz.md", "section": "S"},
            "score": 0.8,
        }]

    s.search.side_effect = _search
    return s


def test_filters_and_versions_do_not_share_cache(fake_store):
    r = Retriever(fake_store)
    r.retrieve("q", filter={"doc_type": "kpi"})
    r.retrieve("q", filter={"doc_type": "glossary"})
    assert fake_store.search.call_count == 2
    fake_store.knowledge_version = "v2"
    r.retrieve("q", filter={"doc_type": "kpi"})
    assert fake_store.search.call_count == 3


def test_filter_key_order_is_stable_and_return_value_is_isolated(fake_store):
    r = Retriever(fake_store)
    first = r.retrieve("q", filter={"a": 1, "b": 2})
    first[0]["content"] = "polluted"
    assert r.retrieve("q", filter={"b": 2, "a": 1})[0]["content"] != "polluted"
    assert fake_store.search.call_count == 1


def test_invalidation_during_inflight_search_cannot_repopulate_cache(fake_store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()

    def search(*a, **k):
        entered.set()
        assert release.wait(5)
        return [{"content": "old", "metadata": {}, "score": .8}]

    fake_store.search.side_effect = search
    r = Retriever(fake_store)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(r.retrieve, "q")
        assert entered.wait(5)
        r.invalidate_cache()
        fake_store.knowledge_version = "v2"
        release.set()
        pending.result()
    assert r.get_stats()["cache_size"] == 0
    r.retrieve("q")
    assert fake_store.search.call_count == 2


# ─────────────────── 缓存 ───────────────────


def test_retrieve_calls_store_on_first_query(fake_store):
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=60)
    docs = r.retrieve("hello")
    assert len(docs) == 1
    fake_store.search.assert_called_once()


def test_retrieve_cache_hit_on_second_query(fake_store):
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=60)
    r.retrieve("hello")
    r.retrieve("hello")
    # store.search 只应被调用一次
    assert fake_store.search.call_count == 1


def test_cache_hit_recorded_in_stats(fake_store):
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=60)
    r.retrieve("hello")
    r.retrieve("hello")
    stats = r.get_stats()
    assert stats["cache_hits"] == 1
    assert stats["store_hits"] == 1  # 第一次 retrieve 才真正调 store


def test_retrieve_ttl_expiry_invalidates_cache(fake_store):
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=0.05)
    r.retrieve("hello")
    time.sleep(0.1)
    r.retrieve("hello")
    # TTL 过期，重新检索
    assert fake_store.search.call_count == 2


def test_cache_key_includes_k_and_threshold(fake_store):
    """k 或 threshold 不同 → 视为不同 cache key。"""
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=60)
    r.retrieve("q")
    r.retrieve("q", k=5)  # k 不同
    r.retrieve("q", score_threshold=0.8)  # threshold 不同
    assert fake_store.search.call_count == 3


def test_lru_eviction_when_cache_full(fake_store):
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=2, cache_ttl=60)
    r.retrieve("a")
    r.retrieve("b")
    r.retrieve("c")  # a 应被淘汰
    r.retrieve("a")  # 重新检索
    assert fake_store.search.call_count == 4


# ─────────────────── 阈值 / 过滤 ───────────────────


def test_retrieve_passes_filter_to_store(fake_store):
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=60)
    r.retrieve("q", filter={"doc_type": "kpi"})
    fake_store.search.assert_called_with(
        "q", k=3, score_threshold=0.4, filter={"doc_type": "kpi"}
    )


# ─────────────────── 超时统计 ───────────────────


def test_retrieve_records_timeout_when_slow(fake_store):
    def slow_search(*args, **kw):
        time.sleep(0.1)
        return []
    fake_store.search.side_effect = slow_search
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=60)
    r.retrieve("q", timeout_s=0.02)  # 期望超时
    stats = r.get_stats()
    assert stats["timeouts"] == 1


def test_retrieve_does_not_timeout_when_fast(fake_store):
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=60)
    r.retrieve("q", timeout_s=5.0)  # 远大于实际耗时
    stats = r.get_stats()
    assert stats["timeouts"] == 0


# ─────────────────── 异常不写缓存 ───────────────────


def test_retrieve_failure_is_not_cached():
    """store 抛异常产生的空结果绝不能写入缓存，否则一次瞬时故障
    会被放大成 TTL 内同问题一律"无知识命中"。"""
    store = MagicMock()
    # 第一次检索抛异常，之后恢复正常
    store.search.side_effect = [RuntimeError("Chroma 抖动"), [{"content": "x", "metadata": {}, "score": 0.9}]]
    r = Retriever(store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=60)

    assert r.retrieve("q") == []
    assert r.get_stats()["cache_size"] == 0  # 异常结果未入缓存

    # 第二次同问题必须真正打到 store（未被缓存的空结果挡住），且拿到正常结果
    out = r.retrieve("q")
    assert len(out) == 1
    assert store.search.call_count == 2


def test_retrieve_empty_result_is_cached(fake_store):
    """正常检索出的空结果（阈值过滤后无命中）仍照常缓存，避免反复打库。"""
    fake_store.search.return_value = []
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=60)
    r.retrieve("q")
    r.retrieve("q")
    assert fake_store.search.call_count == 1
    assert r.get_stats()["cache_size"] == 1


# ─────────────────── stats ───────────────────


def test_get_stats_includes_required_fields(fake_store):
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=60)
    r.retrieve("q1")
    stats = r.get_stats()
    for key in ("cache_hits", "store_hits", "timeouts", "cache_size",
                "hit_rate_pct", "avg_latency_ms"):
        assert key in stats


def test_get_stats_avg_latency_non_negative(fake_store):
    r = Retriever(fake_store, k=3, score_threshold=0.4, cache_max=10, cache_ttl=60)
    r.retrieve("q")
    stats = r.get_stats()
    assert stats["avg_latency_ms"] >= 0
