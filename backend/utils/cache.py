import asyncio
import hashlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from pydantic import BaseModel, TypeAdapter

logger = logging.getLogger(__name__)

DEFAULT_TTL = 300
_MEMORY_CACHE_MAX_SIZE = 1000

_memory_cache: dict[str, dict] = {}
_redis_client = None
_redis_available = False


class _RefLock:
    """带持有者计数的防击穿锁。

    仅用 lock.locked() 判断"空闲"是不够的：协程可能已从 _cache_locks 取到锁、
    但还在 await 队列中等待（此时 locked() 为 False）。若后台清理任务在这时
    回收锁对象，后续请求会创建一把新锁并同时进入计算，single-flight 失效。
    因此用 refs 计数覆盖"已取用未释放"的全生命周期。
    """

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


_cache_locks: dict[str, _RefLock] = {}


def _release_lock_if_idle(key: str) -> None:
    """无人取用（refs==0）且未上锁时才回收锁对象。"""
    lock = _cache_locks.get(key)
    if lock is not None and lock.refs == 0 and not lock.lock.locked():
        _cache_locks.pop(key, None)


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


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _cache_contains(key: str) -> bool:
    """判断缓存键是否已存在且未过期。

    get() 返回 None 可能是「未缓存」也可能是「缓存的值为 None」，
    single-flight 锁内二次检查必须能区分这两种情况，否则
    缓存值为 None 的函数会反复执行（防击穿失效）。
    """
    if _redis_available and _redis_client:
        try:
            return _redis_client.get(key) is not None
        except Exception:
            return False
    entry = _memory_cache.get(key)
    if entry is None:
        return False
    if entry["expires_at"] < time.time():
        del _memory_cache[key]
        _release_lock_if_idle(key)
        return False
    return True


def get(key: str) -> Any | None:
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
        _release_lock_if_idle(key)
        return None
    return entry["data"]


def set(key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    if _redis_available and _redis_client:
        try:
            _redis_client.setex(key, ttl, json.dumps(_to_jsonable(value), default=str))
            return
        except Exception as e:
            logger.warning(f"Redis SET 失败，降级内存: {e}")
    if len(_memory_cache) >= _MEMORY_CACHE_MAX_SIZE:
        now = time.time()
        expired = [k for k, v in _memory_cache.items() if v["expires_at"] < now]
        for k in expired:
            del _memory_cache[k]
            _release_lock_if_idle(k)
        if len(_memory_cache) >= _MEMORY_CACHE_MAX_SIZE:
            oldest_key = next(iter(_memory_cache))
            del _memory_cache[oldest_key]
            _release_lock_if_idle(oldest_key)
    _memory_cache[key] = {"data": value, "expires_at": time.time() + ttl}


def delete(key: str) -> None:
    if _redis_available and _redis_client:
        try:
            _redis_client.delete(key)
        except Exception:
            pass
    _memory_cache.pop(key, None)
    _cache_locks.pop(key, None)


def clear() -> None:
    if _redis_available and _redis_client:
        try:
            _redis_client.flushdb()
        except Exception:
            pass
    _memory_cache.clear()
    _cache_locks.clear()


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
    """缓存装饰器：自动识别并跳过 SQLAlchemy Session 等不可哈希参数。

    约定：被装饰函数的形参名以 "_" 开头的（如 db / session / conn）会被排除在
    缓存 key 之外，避免每次新建 session 都会 cache miss。
    """
    def decorator(func: Callable[..., Awaitable[Any]]):
        sig = inspect.signature(func)
        return_annotation = sig.return_annotation
        return_adapter = (
            None
            if return_annotation in (inspect.Signature.empty, Any)
            else TypeAdapter(return_annotation)
        )
        param_names = list(sig.parameters.keys())

        def _cache_key_args(args: tuple, kwargs: dict) -> tuple:
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            cache_args = []
            cache_kwargs = {}
            for name, value in bound.arguments.items():
                if name.startswith("_") or name in ("db", "session", "conn"):
                    continue
                if name in param_names[: len(args)]:
                    cache_args.append(value)
                else:
                    cache_kwargs[name] = value
            return tuple(cache_args), cache_kwargs

        @wraps(func)
        async def wrapper(*args, **kwargs):
            cache_args, cache_kwargs = _cache_key_args(args, kwargs)
            cache_key = f"{func.__module__}.{func.__name__}:{_make_key(*cache_args, **cache_kwargs)}"
            cached_result = get(cache_key)
            if cached_result is not None:
                logger.debug(f"缓存命中: {cache_key}")
                return (
                    return_adapter.validate_python(cached_result)
                    if return_adapter
                    else cached_result
                )
            key_lock = _cache_locks.get(cache_key)
            if key_lock is None:
                key_lock = _cache_locks.setdefault(cache_key, _RefLock())
            key_lock.refs += 1
            try:
                async with key_lock.lock:
                    # 用存在性判断而非 get() is not None：
                    # 缓存值本身可能为 None，误判会导致 single-flight 失效
                    if _cache_contains(cache_key):
                        cached_result = get(cache_key)
                        return (
                            return_adapter.validate_python(cached_result)
                            if return_adapter
                            else cached_result
                        )
                    result = await func(*args, **kwargs)
                    set(cache_key, result, ttl=ttl)
                    logger.debug(f"已缓存: {cache_key} (TTL={ttl}s)")
                    return result
            finally:
                key_lock.refs -= 1
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
        _cache_locks.pop(k, None)
        count += 1
    return count


def cleanup_memory_cache() -> int:
    now = time.time()
    expired = [k for k, v in _memory_cache.items() if v["expires_at"] < now]
    for k in expired:
        del _memory_cache[k]
        _release_lock_if_idle(k)

    # Redis 模式没有本地缓存条目，但每个唯一请求仍会创建一次防击穿锁。
    # 仅清理无人取用且未持有的孤立锁，避免高基数请求导致锁字典持续增长。
    orphan_locks = [
        key for key, lock in _cache_locks.items()
        if key not in _memory_cache and lock.refs == 0 and not lock.lock.locked()
    ]
    for key in orphan_locks:
        _cache_locks.pop(key, None)
    return len(expired)
