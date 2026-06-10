"""RAG 监控指标聚合：跨进程 stats 共享与读取。

设计：
- retriever 写自己的 stats（retriever 内部）
- tools.py 调 `record_tool_call(hit, error)` 写工具调用 stats
- retriever 在 dump 时合并两个数据源 → 写一个综合 JSON 文件
- FastAPI /metrics 端点读取这个 JSON

为什么用文件而不用 Redis/共享内存？
- 本地开发 + Docker 双环境统一，不依赖外部服务
- 写频次低（节流 5 秒），IO 开销可忽略
- 原子写入（tmp + os.replace）保证读到一致快照
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_STATS_PATH = os.environ.get(
    "RAG_STATS_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "rag_stats.json"),
)
DEFAULT_EVENTS_PATH = os.environ.get(
    "RAG_EVENTS_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "rag_events.jsonl"),
)

# 工具调用级 stats（独立于 retriever）
_tool_lock = threading.Lock()
_tool_stats: dict = {
    "tool_call_count": 0,    # Agent 调用 query_business_knowledge 的总次数
    "tool_no_hit_count": 0,   # 调用后未命中任何业务知识
    "tool_error_count": 0,    # 调用过程中 retriever 抛异常
}

# 结构化事件 logger（独立通道，便于 ELK / Loki 聚合）
_events_logger = logging.getLogger("rag.events")
# 关闭向上传播，避免事件被根 logger 重复打印
_events_logger.propagate = False
_events_logger.setLevel(logging.INFO)

# 可选：JSONL 事件流文件 handler（按需挂载，避免对磁盘造成写放大）
_events_file_handler: Optional[logging.Handler] = None
_events_file_lock = threading.Lock()


def _ensure_events_file_handler(path: str) -> logging.Handler:
    """获取（或创建）JSONL 事件流文件 handler。

    - 单进程单写：使用线程锁串行化
    - 不重复挂载：检查 _events_logger.handlers 中是否已存在同路径 handler
    """
    global _events_file_handler
    with _events_file_lock:
        if _events_file_handler is not None:
            return _events_file_handler
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(target, encoding="utf-8")
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter("%(message)s"))
        _events_logger.addHandler(h)
        _events_file_handler = h
        return h


def enable_event_file_logging(path: str = DEFAULT_EVENTS_PATH) -> None:
    """显式启用 JSONL 事件落盘（默认关闭，避免对磁盘造成写放大）。

    使用示例（在 app.py 启动时调用一次）：
        from rag import metrics
        metrics.enable_event_file_logging("data/rag_events.jsonl")
    """
    _ensure_events_file_handler(path)


def disable_event_file_logging() -> None:
    """移除 JSONL 事件文件 handler（仅供测试用）。"""
    global _events_file_handler
    with _events_file_lock:
        if _events_file_handler is not None:
            _events_logger.removeHandler(_events_file_handler)
            try:
                _events_file_handler.close()
            except Exception:
                pass
            _events_file_handler = None


def record_tool_call(hit: bool, error: bool = False) -> None:
    """tools.py 在 _query 末尾调用，累计工具级指标。"""
    with _tool_lock:
        _tool_stats["tool_call_count"] += 1
        if not hit:
            _tool_stats["tool_no_hit_count"] += 1
        if error:
            _tool_stats["tool_error_count"] += 1
    log_event("tool_call", hit=hit, error=error)


def get_tool_stats() -> dict:
    """读取工具级 stats 快照。"""
    with _tool_lock:
        return dict(_tool_stats)


def reset_tool_stats() -> None:
    """清零（仅供测试用）。"""
    with _tool_lock:
        for k in _tool_stats:
            _tool_stats[k] = 0


# ─────────────────── 结构化事件日志 ───────────────────


def log_event(event_type: str, **fields) -> None:
    """发送一条结构化事件到 'rag.events' logger。

    用法：
        metrics.log_event("retrieval", query="复购率", top1_score=0.82, hits=2, ms=12.3)

    在生产环境配置 rag.events logger 用 JSON handler（如 python-json-logger），
    即可直接被 ELK / Loki / Vector 抓取聚合。

    字段：
    - ts: 事件时间戳（毫秒）
    - event: 事件类型
    - 其余 fields 透传
    """
    payload = {
        "ts": int(time.time() * 1000),
        "event": event_type,
        **fields,
    }
    try:
        # _events_logger 上挂的所有 handler 都会被自动调用
        # 包括：默认控制台输出 + 启用后的 JSONL 文件 handler
        _events_logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        # 日志异常不能影响主流程
        logger.debug("RAG event log 失败: %s", e)


def dump_combined_stats(retriever_stats: dict,
                        tool_stats: Optional[dict] = None,
                        path: str = DEFAULT_STATS_PATH) -> None:
    """把 retriever stats + tool stats 原子写入 JSON。

    Args:
        retriever_stats: Retriever.get_stats() 的返回值。
        tool_stats: tools 端 stats（默认自动读取）。
        path: 输出文件路径。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": time.time(),
        "retriever": retriever_stats,
        "tool": tool_stats if tool_stats is not None else get_tool_stats(),
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, target)


def load_stats(path: str = DEFAULT_STATS_PATH) -> Optional[dict]:
    """读取 stats JSON。文件不存在或解析失败返回 None。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("RAG stats 读取失败: %s", e)
        return None


def render_prometheus(stats: dict) -> str:
    """把 combined stats 渲染为 Prometheus exposition 格式。

    指标命名：
    - rag_query_total: 总查询次数
    - rag_cache_hits_total: 缓存命中次数
    - rag_store_hits_total: 实际检索次数
    - rag_no_results_total: 无命中次数
    - rag_timeouts_total: 超时次数
    - rag_query_latency_ms: 平均延迟（gauge）
    - rag_hit_rate_pct: 缓存命中率
    - rag_score_bucket: Top1 score 分布（5 个 bucket）
    - rag_tool_call_total: Agent 工具调用次数
    - rag_tool_no_hit_total: 工具无命中次数
    - rag_tool_error_total: 工具错误次数
    """
    if not stats:
        return "# no stats available\n"
    r = stats.get("retriever", {})
    t = stats.get("tool", {})

    lines = [
        "# HELP rag_query_total Total RAG queries (cache + store)",
        "# TYPE rag_query_total counter",
        f"rag_query_total {r.get('total_queries', 0)}",
        "",
        "# HELP rag_cache_hits_total Cache hits",
        "# TYPE rag_cache_hits_total counter",
        f"rag_cache_hits_total {r.get('cache_hits', 0)}",
        "",
        "# HELP rag_store_hits_total Actual store retrievals",
        "# TYPE rag_store_hits_total counter",
        f"rag_store_hits_total {r.get('store_hits', 0)}",
        "",
        "# HELP rag_no_results_total Retrievals that returned 0 docs",
        "# TYPE rag_no_results_total counter",
        f"rag_no_results_total {r.get('no_results', 0)}",
        "",
        "# HELP rag_timeouts_total Slow retrievals exceeding timeout",
        "# TYPE rag_timeouts_total counter",
        f"rag_timeouts_total {r.get('timeouts', 0)}",
        "",
        "# HELP rag_query_latency_ms Average actual retrieval latency",
        "# TYPE rag_query_latency_ms gauge",
        f"rag_query_latency_ms {r.get('avg_latency_ms', 0)}",
        "",
        "# HELP rag_hit_rate_pct Cache hit rate percentage",
        "# TYPE rag_hit_rate_pct gauge",
        f"rag_hit_rate_pct {r.get('hit_rate_pct', 0)}",
        "",
        "# HELP rag_score_bucket Top1 retrieval score distribution",
        "# TYPE rag_score_bucket counter",
    ]
    for bucket, count in (r.get("score_buckets") or {}).items():
        lines.append(f'rag_score_bucket{{range="{bucket}"}} {count}')

    lines.extend([
        "",
        "# HELP rag_tool_call_total Agent invoked query_business_knowledge",
        "# TYPE rag_tool_call_total counter",
        f"rag_tool_call_total {t.get('tool_call_count', 0)}",
        "",
        "# HELP rag_tool_no_hit_total Tool returned no knowledge",
        "# TYPE rag_tool_no_hit_total counter",
        f"rag_tool_no_hit_total {t.get('tool_no_hit_count', 0)}",
        "",
        "# HELP rag_tool_error_total Tool execution errors",
        "# TYPE rag_tool_error_total counter",
        f"rag_tool_error_total {t.get('tool_error_count', 0)}",
        "",
    ])
    return "\n".join(lines)
