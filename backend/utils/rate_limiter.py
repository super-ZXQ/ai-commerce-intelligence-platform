import logging
import time
from collections import defaultdict

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_rate_store: dict[str, list[float]] = defaultdict(list)

RATE_LIMITS = {
    "/api/auth/login": {"requests": 5, "window": 60},
    "/api/ai/query": {"requests": 10, "window": 60},
    "/api/export/": {"requests": 3, "window": 120},
    "default": {"requests": 100, "window": 60},
}


def _get_client_id(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request) -> dict:
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


async def rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/docs") or request.url.path.startswith("/redoc") or request.url.path == "/health" or request.url.path == "/" or request.url.path == "/demo":
        response = await call_next(request)
        return response

    try:
        info = check_rate_limit(request)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(info["limit"])
        response.headers["X-RateLimit-Remaining"] = str(info["remaining"])
        response.headers["X-RateLimit-Reset"] = str(info["reset"])
        return response
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error_code": "RATE_LIMITED",
                "message": exc.detail,
            },
            headers={k: v for k, v in (exc.headers or {}).items()},
        )
