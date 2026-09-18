"""进程内订单事件与降级指标。

Prometheus 抓取的是进程快照；单实例 Docker 场景足够用于可复现实验。多副本部署
应由 Prometheus 汇聚各副本，而不是在应用内伪造全局计数。
"""

from __future__ import annotations

import threading
from collections import Counter

_lock = threading.Lock()
_received: Counter[str] = Counter()
_processed: Counter[str] = Counter()
_processing_seconds_sum = 0.0
_processing_seconds_count = 0
_lag_seconds = 0.0
_db_timeouts = 0


def record_event(event_type: str, result: str, duration_seconds: float, lag_seconds: float) -> None:
    global _processing_seconds_sum, _processing_seconds_count, _lag_seconds
    with _lock:
        _received[event_type] += 1
        _processed[result] += 1
        _processing_seconds_sum += max(duration_seconds, 0.0)
        _processing_seconds_count += 1
        _lag_seconds = max(lag_seconds, 0.0)


def record_db_timeout() -> None:
    global _db_timeouts
    with _lock:
        _db_timeouts += 1


def snapshot() -> dict:
    with _lock:
        return {
            "received": dict(_received),
            "processed": dict(_processed),
            "processing_seconds_sum": _processing_seconds_sum,
            "processing_seconds_count": _processing_seconds_count,
            "lag_seconds": _lag_seconds,
            "db_timeouts": _db_timeouts,
        }


def render_prometheus() -> str:
    data = snapshot()
    lines = [
        "# TYPE order_events_received_total counter",
        "# TYPE order_events_processed_total counter",
    ]
    lines.extend(
        f'order_events_received_total{{type="{kind}"}} {count}'
        for kind, count in sorted(data["received"].items())
    )
    lines.extend(
        f'order_events_processed_total{{result="{result}"}} {count}'
        for result, count in sorted(data["processed"].items())
    )
    lines.extend([
        "# TYPE order_event_processing_seconds summary",
        f"order_event_processing_seconds_sum {data['processing_seconds_sum']}",
        f"order_event_processing_seconds_count {data['processing_seconds_count']}",
        "# TYPE order_event_lag_seconds gauge",
        f"order_event_lag_seconds {data['lag_seconds']}",
        "# TYPE db_timeouts_total counter",
        f"db_timeouts_total {data['db_timeouts']}",
    ])
    return "\n".join(lines) + "\n"
