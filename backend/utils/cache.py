import json
import logging
import hashlib
import time
from typing import Any, Optional, Callable, Awaitable
from functools import wraps

logger = logging.getLogger(__name__)

DEFAULT_TTL = 300

_memory_cache: dict[str, dict] = {}

_redis_client = None
_redis_available = False


def init_redis(redis_url: str) -> bool:
    global _redis_client, _redis_available
    try:
        import redis
        _redis_client = redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
        _redis_client.ping()
        _redis_available = True
        logger.info("✅ Redis 缓存已连接")
        return True
    except Exception as e:
        _redis_available = False
        _redis_client = None
        logger.warning(f"⚠️ Redis 连接失败，降级为内存缓存: {e}")
        return False


def check_redis_health() -> dict:
    if not _redis_available or _redis_client is None:
        return {"status": "disabled", "backend": "memory"}
    try:
        _redis_client.ping()
        info = _redis_client.info("memory")
        return {
            "status": "ok",
            "backend": "redis",
            "used_memory_human": info.get("used_memory_human", "N/A"),
            "keys": _redis_client.dbsize(),
        }
    except Exception as e:
        return {"status": "error", "detail": str(e), "backend": "redis_fallback_memory"}


def _make_key(*args, **kwargs) -> str:
    raw = json.dumps({"a": args, "k": kwargs}, sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


def get(key: str) -> Optional[Any]:
    if _redis_available and _redis_client:
        try:
            raw = _redis_client.get(key)
            if raw is not None:
                return json.loads(raw)
            return None
        except Exception as e:
            logger.warning(f"Redis GET 失败，降级内存: {e}")
    entry = _memory_cache.get(key)
    if entry is None:
        return None
    if entry["expires_at"] < time.time():
        del _memory_cache[key]
        return None
    return entry["data"]


def set(key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    if _redis_available and _redis_client:
        try:
            _redis_client.setex(key, ttl, json.dumps(value, default=str))
            return
        except Exception as e:
            logger.warning(f"Redis SET 失败，降级内存: {e}")
    _memory_cache[key] = {"data": value, "expires_at": time.time() + ttl}


def delete(key: str) -> None:
    if _redis_available and _redis_client:
        try:
            _redis_client.delete(key)
        except Exception:
            pass
    _memory_cache.pop(key, None)


def clear() -> None:
    if _redis_available and _redis_client:
        try:
            _redis_client.flushdb()
        except Exception:
            pass
    _memory_cache.clear()


def stats() -> dict:
    if _redis_available and _redis_client:
        try:
            _redis_client.ping()
            info = _redis_client.info("memory")
            return {
                "status": "ok",
                "backend": "redis",
                "keys": _redis_client.dbsize(),
                "memory_human": info.get("used_memory_human", "N/A"),
            }
        except Exception:
            return {"status": "error", "backend": "redis_fallback_memory", "keys": len(_memory_cache)}
    return {"status": "ok", "backend": "memory", "keys": len(_memory_cache)}


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
    count = 0
    if _redis_available and _redis_client:
        try:
            cursor = 0
            while True:
                cursor, keys = _redis_client.scan(cursor, match=f"*{pattern}*", count=100)
                if keys:
                    count += _redis_client.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            pass
    keys_to_delete = [k for k in _memory_cache if pattern in k]
    for k in keys_to_delete:
        del _memory_cache[k]
        count += 1
    return count


def cleanup_memory_cache() -> int:
    now = time.time()
    expired = [k for k, v in _memory_cache.items() if v["expires_at"] < now]
    for k in expired:
        del _memory_cache[k]
    return len(expired)
