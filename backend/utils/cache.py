import json
import logging
import hashlib
import time
from typing import Any, Optional, Callable, Awaitable
from functools import wraps

logger = logging.getLogger(__name__)

_cache: dict[str, dict] = {}
DEFAULT_TTL = 300


def _make_key(*args, **kwargs) -> str:
    raw = json.dumps({"a": args, "k": kwargs}, sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


def get(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if entry is None:
        return None
    if entry["expires_at"] < time.time():
        del _cache[key]
        return None
    return entry["data"]


def set(key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    _cache[key] = {"data": value, "expires_at": time.time() + ttl}


def delete(key: str) -> None:
    _cache.pop(key, None)


def clear() -> None:
    _cache.clear()


def stats() -> dict:
    return {"keys": len(_cache), "memory_estimate_mb": len(json.dumps(_cache, default=str)) / (1024 * 1024)}


def cached(ttl: int = DEFAULT_TTL):
    def decorator(func: Callable[..., Awaitable[Any]]):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            cache_key = f"{func.__module__}.{func.__name__}:{_make_key(*args[1:], **kwargs)}"
            cached_result = get(cache_key)
            if cached_result is not None:
                logger.debug(f"缓存命中: {cache_key}")
                return cached_result
            result = await func(*args, **kwargs)
            set(cache_key, result, ttl=ttl)
            logger.debug(f"已缓存: {cache_key} (TTL={ttl}s)")
            return result
        return wrapper
    return decorator


def invalidate_pattern(pattern: str) -> int:
    keys_to_delete = [k for k in _cache if pattern in k]
    for k in keys_to_delete:
        delete(k)
    return len(keys_to_delete)
