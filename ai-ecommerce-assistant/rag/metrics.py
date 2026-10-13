"""RAG 监控指标聚合：跨进程 stats 共享与读取。

设计：
- retriever 写自己的 stats（retriever 内部）
- retriever 在 dump 时原子写入综合 JSON 文件
- FastAPI /api/monitor/rag-stats 端点读取这个 JSON 并渲染 Prometheus 格式

为什么用文件而不用 Redis/共享内存？
- 本地开发 + Docker 双环境统一，不依赖外部服务
- 写频次低（节流 5 秒），IO 开销可忽略
- 原子写入（tmp + os.replace）保证读到一致快照
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STATS_PATH = os.environ.get(
    "RAG_STATS_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "rag_stats.json"),
)
DEFAULT_EVENTS_PATH = os.environ.get(
    "RAG_EVENTS_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "rag_events.jsonl"),
)

# 结构化事件 logger（独立通道，便于 ELK / Loki 聚合）
_events_logger = logging.getLogger("rag.events")
# 关闭向上传播，避免事件被根 logger 重复打印
_events_logger.propagate = False
_events_logger.setLevel(logging.INFO)

# 可选：JSONL 事件流文件 handler（按需挂载，避免对磁盘造成写放大）
_events_file_handler: logging.Handler | None = None
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
        h = RotatingFileHandler(target, maxBytes=1024 * 1024, backupCount=4, encoding="utf-8")
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
    # 保留事件结构与观测指标，隐去问题内容和可能包含数据的SQL/错误文本。
    for key in ("query", "sql", "answer", "error"):
        if key in fields:
            fields[key + "_length"] = len(str(fields[key]))
            fields[key] = "[redacted]"
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
                        path: str = DEFAULT_STATS_PATH) -> None:
    """把 retriever stats 原子写入 JSON。

    Args:
        retriever_stats: Retriever.get_stats() 的返回值。
        path: 输出文件路径。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": time.time(),
        "retriever": retriever_stats,
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, target)


def load_stats(path: str = DEFAULT_STATS_PATH) -> dict | None:
    """读取 stats JSON。文件不存在或解析失败返回 None。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("RAG stats 读取失败: %s", e)
        return None
