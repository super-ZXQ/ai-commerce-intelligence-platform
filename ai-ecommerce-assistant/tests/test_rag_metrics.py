"""P6 监控与埋点测试。"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from rag import metrics
from rag.retriever import Retriever, _bucketize


@pytest.fixture(autouse=True)
def reset_tool_stats():
    """每个测试前清零，避免污染。"""
    metrics.reset_tool_stats()
    yield
    metrics.reset_tool_stats()


# ─────────── _bucketize ───────────


@pytest.mark.parametrize("score,expected", [
    (0.0, "[0,0.2)"),
    (0.19, "[0,0.2)"),
    (0.2, "[0.2,0.4)"),
    (0.5, "[0.4,0.6)"),
    (0.7, "[0.6,0.8)"),
    (0.9, "[0.8,1.0]"),
    (1.0, "[0.8,1.0]"),
])
def test_bucketize_boundaries(score, expected):
    assert _bucketize(score) == expected


# ─────────── record_tool_call / get_tool_stats ───────────


def test_record_tool_call_hit():
    metrics.record_tool_call(hit=True)
    s = metrics.get_tool_stats()
    assert s["tool_call_count"] == 1
    assert s["tool_no_hit_count"] == 0
    assert s["tool_error_count"] == 0


def test_record_tool_call_no_hit():
    metrics.record_tool_call(hit=False)
    s = metrics.get_tool_stats()
    assert s["tool_no_hit_count"] == 1


def test_record_tool_call_error():
    metrics.record_tool_call(hit=False, error=True)
    s = metrics.get_tool_stats()
    assert s["tool_error_count"] == 1
    assert s["tool_no_hit_count"] == 1


def test_record_tool_call_thread_safe():
    """并发调用不应丢计数。"""
    import threading
    barrier = threading.Barrier(100)

    def worker():
        barrier.wait()
        for _ in range(10):
            metrics.record_tool_call(hit=True)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = metrics.get_tool_stats()
    assert s["tool_call_count"] == 1000


def test_reset_tool_stats():
    metrics.record_tool_call(hit=True)
    metrics.reset_tool_stats()
    s = metrics.get_tool_stats()
    assert s["tool_call_count"] == 0


# ─────────── dump_combined_stats / load_stats ───────────


def test_dump_and_load_atomic(tmp_path):
    """dump + load 应产生等价 payload。"""
    p = tmp_path / "stats.json"
    r_stats = {"total_queries": 5, "hit_rate_pct": 80.0}
    t_stats = {"tool_call_count": 3}
    metrics.dump_combined_stats(r_stats, t_stats, path=str(p))
    loaded = metrics.load_stats(str(p))
    assert loaded["retriever"] == r_stats
    assert loaded["tool"] == t_stats
    assert "timestamp" in loaded


def test_dump_creates_parent_dir(tmp_path):
    """目标目录不存在时自动创建。"""
    p = tmp_path / "nested" / "deep" / "stats.json"
    metrics.dump_combined_stats({}, {}, path=str(p))
    assert p.exists()


def test_load_returns_none_if_missing(tmp_path):
    p = tmp_path / "not_exist.json"
    assert metrics.load_stats(str(p)) is None


def test_load_returns_none_on_corrupted(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    assert metrics.load_stats(str(p)) is None


def test_dump_uses_atomic_rename(tmp_path):
    """dump 时应有 .tmp 文件，rename 后消失。"""
    p = tmp_path / "stats.json"
    metrics.dump_combined_stats({}, {}, path=str(p))
    # 最终 .tmp 不应残留
    assert not (tmp_path / "stats.json.tmp").exists()
    assert p.exists()


# ─────────── log_event ───────────


def test_log_event_emits_json(monkeypatch):
    """log_event 应输出 JSON 格式日志到 rag.events logger。"""
    import logging
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _Capture(level=logging.INFO)
    logger = logging.getLogger("rag.events")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        metrics.log_event("retrieval", query="复购率", top1_score=0.82)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert any("retrieval" in c for c in captured), f"捕获日志: {captured}"
    msg = next(c for c in captured if "retrieval" in c)
    payload = json.loads(msg)
    assert payload["event"] == "retrieval"
    assert payload["query"] == "复购率"
    assert payload["top1_score"] == 0.82
    assert "ts" in payload


def test_log_event_swallows_logging_errors(monkeypatch):
    """log_event 内部异常不应抛出。"""
    fake_logger = MagicMock()
    fake_logger.info.side_effect = RuntimeError("boom")
    monkeypatch.setattr(metrics, "_events_logger", fake_logger)
    metrics.log_event("test")  # 不应抛


# ─────────── render_prometheus ───────────


def test_render_prometheus_empty():
    out = metrics.render_prometheus(None)
    assert "# no stats available" in out


def test_render_prometheus_full():
    stats = {
        "retriever": {
            "total_queries": 10,
            "cache_hits": 3,
            "store_hits": 7,
            "no_results": 2,
            "timeouts": 1,
            "avg_latency_ms": 12.5,
            "hit_rate_pct": 30.0,
            "score_buckets": {
                "[0,0.2)": 0, "[0.2,0.4)": 1, "[0.4,0.6)": 2,
                "[0.6,0.8)": 3, "[0.8,1.0]": 4,
            },
        },
        "tool": {
            "tool_call_count": 5, "tool_no_hit_count": 1, "tool_error_count": 0,
        },
    }
    out = metrics.render_prometheus(stats)
    # 关键指标
    assert "rag_query_total 10" in out
    assert "rag_cache_hits_total 3" in out
    assert "rag_store_hits_total 7" in out
    assert "rag_no_results_total 2" in out
    assert "rag_timeouts_total 1" in out
    assert "rag_query_latency_ms 12.5" in out
    assert "rag_hit_rate_pct 30" in out
    assert "rag_tool_call_total 5" in out
    assert "rag_tool_no_hit_total 1" in out
    # score bucket 标签
    assert 'range="[0.8,1.0]"' in out
    assert "rag_score_bucket" in out
    # Prometheus 协议头
    assert "# TYPE rag_query_total counter" in out
    assert "text/plain" not in out  # 不应在文本里


# ─────────── Retriever 集成 ───────────


@pytest.fixture
def fake_store():
    s = MagicMock()
    s.count.return_value = 0

    def _search(query, k=3, score_threshold=None, filter=None):
        # 根据 query 返回不同 score，便于测 score_buckets
        if "low" in query:
            return [{"content": "x", "metadata": {"source": "a.md"}, "score": 0.1}]
        if "high" in query:
            return [{"content": "x", "metadata": {"source": "a.md"}, "score": 0.95}]
        return [{"content": "x", "metadata": {"source": "a.md"}, "score": 0.5}]

    s.search.side_effect = _search
    return s


def test_retriever_score_buckets_increments(tmp_path, fake_store):
    r = Retriever(
        fake_store, k=3, score_threshold=0.0,
        stats_path=str(tmp_path / "stats.json"),
    )
    r.retrieve("low relevance question")    # 0.1 → [0,0.2)
    r.retrieve("another low one")           # 0.1 → [0,0.2)
    r.retrieve("medium score")              # 0.5 → [0.4,0.6)
    r.retrieve("high score query")          # 0.95 → [0.8,1.0]
    s = r.get_stats()
    assert s["score_buckets"]["[0,0.2)"] == 2
    assert s["score_buckets"]["[0.4,0.6)"] == 1
    assert s["score_buckets"]["[0.8,1.0]"] == 1


def test_retriever_no_results_counted(tmp_path, fake_store):
    fake_store.search.side_effect = None
    fake_store.search.return_value = []
    r = Retriever(
        fake_store, k=3, score_threshold=0.0,
        stats_path=str(tmp_path / "stats.json"),
    )
    r.retrieve("q1")
    r.retrieve("q2")
    assert r.get_stats()["no_results"] == 2


def test_retriever_total_queries_field(tmp_path, fake_store):
    r = Retriever(
        fake_store, k=3, score_threshold=0.0,
        stats_path=str(tmp_path / "stats.json"),
    )
    r.retrieve("a")
    r.retrieve("a")  # cache hit
    r.retrieve("b")
    assert r.get_stats()["total_queries"] == 3


def test_retriever_dump_stats_creates_file(tmp_path, fake_store):
    p = tmp_path / "rag_stats.json"
    r = Retriever(
        fake_store, k=3, score_threshold=0.0,
        stats_path=str(p), dump_interval_s=0,
    )
    r.retrieve("hello")
    r.dump_stats()  # 强制立即写
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "retriever" in data
    assert "tool" in data
    assert "timestamp" in data
    assert data["retriever"]["total_queries"] >= 1


def test_retriever_dump_stats_overwrites(tmp_path, fake_store):
    p = tmp_path / "rag_stats.json"
    r = Retriever(
        fake_store, k=3, score_threshold=0.0,
        stats_path=str(p), dump_interval_s=0,
    )
    r.retrieve("a")
    r.dump_stats()
    t1 = (p.stat().st_mtime_ns, p.read_text())
    time.sleep(0.05)
    r.retrieve("b")
    r.dump_stats()
    t2 = (p.stat().st_mtime_ns, p.read_text())
    assert t1[0] < t2[0]
    assert t1[1] != t2[1]


# ─────────── JSONL 事件文件 handler ───────────


def test_enable_event_file_logging_writes_jsonl(tmp_path):
    """enable_event_file_logging 后 log_event 应追加到 JSONL 文件。"""
    metrics.disable_event_file_logging()  # 保险
    p = tmp_path / "events.jsonl"
    metrics.enable_event_file_logging(str(p))
    try:
        metrics.log_event("retrieval", query="复购率", top1_score=0.82, hits=2, ms=12.3)
        metrics.log_event("tool_call", hit=True)
        # 触发 handler 写盘（FileHandler 是延迟 emit 的，强制 flush）
        h = metrics._events_file_handler
        if h is not None:
            h.flush()
    finally:
        metrics.disable_event_file_logging()
    # 至少一条记录
    assert p.exists()
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2
    first = json.loads(lines[0])
    assert first["event"] == "retrieval"
    assert first["query"] == "复购率"
    assert first["top1_score"] == 0.82
    second = json.loads(lines[1])
    assert second["event"] == "tool_call"
    assert second["hit"] is True


def test_enable_event_file_logging_idempotent(tmp_path):
    """重复调用不应挂多个 handler。"""
    p = tmp_path / "events.jsonl"
    metrics.disable_event_file_logging()
    metrics.enable_event_file_logging(str(p))
    h1 = metrics._events_file_handler
    metrics.enable_event_file_logging(str(p))  # 第二次
    h2 = metrics._events_file_handler
    metrics.disable_event_file_logging()
    # 两次拿到的是同一个 handler，没有重复挂载
    assert h1 is h2


def test_disable_event_file_logging_removes_handler(tmp_path):
    p = tmp_path / "events.jsonl"
    metrics.enable_event_file_logging(str(p))
    assert metrics._events_file_handler is not None
    metrics.disable_event_file_logging()
    assert metrics._events_file_handler is None


def test_disable_event_file_logging_safe_when_disabled():
    """未启用时调用 disable 不应抛。"""
    metrics.disable_event_file_logging()
    metrics.disable_event_file_logging()  # 不应抛


# ─────────── app.py record_feedback 写文件 ───────────


def test_record_feedback_writes_jsonl(tmp_path, monkeypatch):
    """app.record_feedback 应把反馈追加到 JSONL 文件。"""
    import sys
    sys.path.insert(0, str(tmp_path))  # 保险
    fb_path = tmp_path / "feedback.jsonl"
    monkeypatch.setenv("RAG_FEEDBACK_PATH", str(fb_path))
    # 重新加载 app 模块以让环境变量生效
    if "app" in sys.modules:
        del sys.modules["app"]
    from app import record_feedback
    ok1 = record_feedback("什么是复购率？", "复购率 = ...", "up",
                          rag_sources=[{"filename": "a.md"}, {"filename": "b.md"}])
    ok2 = record_feedback("查下销量", "暂无数据", "down",
                          rag_sources=[])
    assert ok1 is True
    assert ok2 is True
    lines = fb_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    p1 = json.loads(lines[0])
    assert p1["rating"] == "up"
    assert p1["question"] == "什么是复购率？"
    assert p1["rag_sources_count"] == 2
    assert p1["rag_filenames"] == ["a.md", "b.md"]
    p2 = json.loads(lines[1])
    assert p2["rating"] == "down"
    assert p2["rag_sources_count"] == 0
    assert p2["rag_filenames"] == []
