import logging
import time
import threading
from collections import defaultdict

from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)

_rate_store: dict[str, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()

RATE_LIMITS = {
    "/api/auth/login": {"requests": 5, "window": 60},
    "/api/ai/query": {"requests": 10, "window": 60},
    "/api/export/": {"requests": 3, "window": 120},
    "default": {"requests": 100, "window": 60},
}

_CLEANUP_INTERVAL = 300
_MAX_STORE_SIZE = 10000


def _cleanup_expired_keys() -> None:
    now = time.time()
    max_window = max(cfg["window"] for cfg in RATE_LIMITS.values())
    expired_keys = []
    for key, timestamps in _rate_store.items():
        _rate_store[key] = [t for t in timestamps if now - t < max_window]
        if not _rate_store[key]:
            expired_keys.append(key)
    for key in expired_keys:
        del _rate_store[key]


def _periodic_cleanup() -> None:
    while True:
        time.sleep(_CLEANUP_INTERVAL)
        with _rate_lock:
            _cleanup_expired_keys()
            if len(_rate_store) > _MAX_STORE_SIZE:
                sorted_keys = sorted(_rate_store.keys(), key=lambda k: len(_rate_store[k]))
                for key in sorted_keys[: len(_rate_store) - _MAX_STORE_SIZE]:
                    del _rate_store[key]
                logger.info(f"限流器清理: 保留 {len(_rate_store)}/{_MAX_STORE_SIZE} 条记录")


_cleanup_thread_started = False


def _ensure_cleanup_thread():
    global _cleanup_thread_started
    if not _cleanup_thread_started:
        _cleanup_thread_started = True
        t = threading.Thread(target=_periodic_cleanup, daemon=True)
        t.start()


def _get_client_id(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ips = [ip.strip() for ip in forwarded.split(",")]
        if ips:
            return ips[-1]
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request) -> dict:
    _ensure_cleanup_thread()
    path = request.url.path
    limit_config = RATE_LIMITS.get(path)
    if not limit_config:
        for pattern, config in RATE_LIMITS.items():
            if pattern != "default" and path.startswith(pattern):
                limit_config = config
                break
    if not limit_config:
        limit_config = RATE_LIMITS["default"]

    client_id = _get_client_id(request)
    key = f"{client_id}:{path}"
    now = time.time()
    window = limit_config["window"]
    max_requests = limit_config["requests"]

    with _rate_lock:
        _rate_store[key] = [t for t in _rate_store[key] if now - t < window]

        if len(_rate_store[key]) >= max_requests:
            retry_after = int(_rate_store[key][0] + window - now) + 1
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁，请 {retry_after} 秒后重试",
                headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(max_requests)},
            )

        _rate_store[key].append(now)

        remaining = max_requests - len(_rate_store[key])
        return {
            "limit": max_requests,
            "remaining": remaining,
            "reset": int(_rate_store[key][0] + window) if _rate_store[key] else int(now + window),
        }
