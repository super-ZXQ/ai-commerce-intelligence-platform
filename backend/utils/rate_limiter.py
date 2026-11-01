import logging
import threading
import time
from collections import defaultdict

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_rate_store: dict[str, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()
# 守护清理线程一次性启动，避免多线程并发首次调用时启动多个 daemon
_init_lock = threading.Lock()

RATE_LIMITS = {
    "/api/auth/login": {"requests": 5, "window": 60},
    "/api/ai/query": {"requests": 10, "window": 60},
    "/api/order-events": {"requests": 120, "window": 60},
    "/api/export/": {"requests": 3, "window": 120},
    "default": {"requests": 100, "window": 60},
}

# 主体级限流：同一 NAT/代理后的多个账号不能互相挤占额度，单个账号也不能
# 通过切换流式/非流式端点绕过配额。桶名固定，与具体 URL 路径解耦。
PRINCIPAL_RATE_LIMITS = {
    "ai": {
        "prefixes": ("/api/ai/query", "/api/ai/stream"),
        "requests": 10,
        "window": 60,
    },
    "export": {
        "prefixes": ("/api/export/",),
        "requests": 3,
        "window": 120,
    },
    "events": {
        "prefixes": ("/api/order-events",),
        "requests": 120,
        "window": 60,
    },
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
    if _cleanup_thread_started:
        return
    with _init_lock:
        if _cleanup_thread_started:
            return
        _cleanup_thread_started = True
        t = threading.Thread(target=_periodic_cleanup, daemon=True)
        t.start()


def _get_client_id(request: Request, trust_proxy_headers: bool = False) -> str:
    if trust_proxy_headers:
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # 取最右侧一跳：链路为 "客户端伪造值, ..., 可信代理追加的真实IP"，
            # 第一个 IP 完全由客户端可控，用它做限流键可被任意绕过。
            ips = [ip.strip() for ip in forwarded.split(",")]
            if ips:
                return ips[-1]
    return request.client.host if request.client else "unknown"


def check_principal_rate_limit(request: Request, principal_name: str) -> dict | None:
    """按调用主体限流；未命中受控桶的路由返回 None。

    与 IP 限流同时生效：IP 桶防止匿名滥用，主体桶防止同一出口下的
    单账号挤占其他账号的额度。
    """
    if not principal_name:
        return None
    path = request.url.path
    bucket = None
    for name, config in PRINCIPAL_RATE_LIMITS.items():
        if any(path.startswith(prefix) for prefix in config["prefixes"]):
            bucket = name
            break
    if bucket is None:
        return None

    _ensure_cleanup_thread()
    config = PRINCIPAL_RATE_LIMITS[bucket]
    key = f"principal:{principal_name}:{bucket}"
    now = time.time()
    window = config["window"]
    max_requests = config["requests"]

    with _rate_lock:
        _rate_store[key] = [t for t in _rate_store[key] if now - t < window]
        if len(_rate_store[key]) >= max_requests:
            retry_after = int(_rate_store[key][0] + window - now) + 1
            raise HTTPException(
                status_code=429,
                detail=f"该账号请求过于频繁，请 {retry_after} 秒后重试",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(max_requests),
                    "X-RateLimit-Scope": "principal",
                },
            )
        _rate_store[key].append(now)
        return {
            "limit": max_requests,
            "remaining": max_requests - len(_rate_store[key]),
            "reset": int(_rate_store[key][0] + window),
        }


def check_rate_limit(request: Request, trust_proxy_headers: bool = False) -> dict:
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

    client_id = _get_client_id(request, trust_proxy_headers=trust_proxy_headers)
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
