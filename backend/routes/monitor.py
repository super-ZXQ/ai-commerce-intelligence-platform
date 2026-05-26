import logging
import time
import os
from datetime import datetime, timezone

from fastapi import APIRouter

from backend.database import check_db_connection
from backend.utils.cache import _memory_cache, check_redis_health

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor", tags=["监控"])

_start_time = time.time()
_request_stats = {
    "total": 0,
    "success": 0,
    "error": 0,
    "by_endpoint": {},
}


def record_request(endpoint: str, status_code: int, duration_ms: float):
    _request_stats["total"] += 1
    if 200 <= status_code < 400:
        _request_stats["success"] += 1
    else:
        _request_stats["error"] += 1
    if endpoint not in _request_stats["by_endpoint"]:
        _request_stats["by_endpoint"][endpoint] = {"count": 0, "errors": 0, "total_ms": 0}
    ep = _request_stats["by_endpoint"][endpoint]
    ep["count"] += 1
    ep["total_ms"] += duration_ms
    if status_code >= 400:
        ep["errors"] += 1


@router.get("/metrics", summary="监控指标")
async def get_metrics():
    uptime_sec = int(time.time() - _start_time)
    top_endpoints = sorted(
        _request_stats["by_endpoint"].items(),
        key=lambda x: x[1]["count"],
        reverse=True,
    )[:10]

    return {
        "server": {
            "status": "running",
            "uptime_seconds": uptime_sec,
            "started_at": datetime.fromtimestamp(_start_time, tz=timezone.utc).isoformat(),
            "python_version": os.sys.version.split()[0],
            "process_id": os.getpid(),
        },
        "requests": {
            "total": _request_stats["total"],
            "success": _request_stats["success"],
            "error": _request_stats["error"],
            "error_rate": round(_request_stats["error"] / max(_request_stats["total"], 1) * 100, 2),
            "top_endpoints": [
                {
                    "path": path,
                    "count": stats["count"],
                    "avg_ms": round(stats["total_ms"] / max(stats["count"], 1), 1),
                    "errors": stats["errors"],
                }
                for path, stats in top_endpoints
            ],
        },
        "cache": {"keys": len(_memory_cache)} if _memory_cache is not None else {"keys": 0},
    }


@router.get("/health/detailed", summary="详细健康检查")
async def detailed_health():
    checks = {}
    try:
        checks["database"] = {"status": "ok" if await check_db_connection() else "error"}
    except Exception as e:
        checks["database"] = {"status": "error", "detail": str(e)}

    try:
        checks["cache"] = {"status": "ok", "keys": len(_memory_cache)}
    except Exception:
        checks["cache"] = {"status": "error"}

    try:
        checks["redis"] = check_redis_health()
    except Exception as e:
        checks["redis"] = {"status": "error", "detail": str(e)}

    all_ok = all(c.get("status") == "ok" for c in checks.values())
    return {
        "status": "healthy" if all_ok else "degraded",
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
