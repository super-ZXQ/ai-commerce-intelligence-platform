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
    s.count.return_value = 1

    def _search(query, k=3, score_threshold=None, filter=None):
        return [{
            "content": f"doc for {query}",
            "metadata": {"source": "biz.md", "section": "S"},
            "score": 0.8,
        }]

    s.search.side_effect = _search
    return s


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


# ─────────────────── format_context ───────────────────


def test_format_context_empty():
    assert Retriever.format_context([]) == ""


def test_format_context_includes_source_and_section():
    docs = [{
        "content": "x",
        "metadata": {"source": "biz.md", "section": "s1"},
        "score": 0.9,
    }]
    out = Retriever.format_context(docs)
    assert "biz.md" in out
    assert "s1" in out
    assert "x" in out


def test_format_context_includes_score_when_requested():
    docs = [{
        "content": "x",
        "metadata": {"source": "biz.md", "section": "s1"},
        "score": 0.91,
    }]
    out = Retriever.format_context(docs, include_score=True)
    assert "0.91" in out


def test_format_context_omits_score_by_default():
    """默认不显示分数，避免冗余。"""
    docs = [{
        "content": "x",
        "metadata": {"source": "biz.md", "section": "s1"},
        "score": 0.91,
    }]
    out = Retriever.format_context(docs)
    assert "0.91" not in out


def test_format_context_truncates_when_exceeds_max_chars():
    docs = [{
        "content": "a" * 5000,
        "metadata": {"source": "b.md", "section": "s"},
        "score": 0.5,
    }]
    out = Retriever.format_context(docs, max_total_chars=500)
    # 允许几十字符的标题余量
    assert len(out) <= 600


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
