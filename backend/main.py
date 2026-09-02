import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from backend.config import get_settings
from backend.database import check_db_connection, engine
from backend.routes import ai, analytics, auth, export, monitor, orders, products, rfm
from backend.routes.monitor import (
    _load_rag_stats,
    detailed_health,
    record_request,
    render_backend_prometheus,
    render_rag_prometheus,
)
from backend.services.rfm_service import RfmDataUnavailableError
from backend.utils.cache import cleanup_memory_cache, init_redis
from backend.utils.cache import clear as clear_cache
from backend.utils.rate_limiter import check_rate_limit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()


_CACHE_CLEANUP_INTERVAL = 300


async def _cache_cleanup_task():
    while True:
        await asyncio.sleep(_CACHE_CLEANUP_INTERVAL)
        try:
            removed = cleanup_memory_cache()
            if removed > 0:
                logger.info(f"🧹 内存缓存清理: 移除 {removed} 个过期键")
        except Exception:
            logger.exception("缓存清理循环发生未预期异常")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 AI Commerce Intelligence Platform v%s 启动中...", settings.app_version)
    if not await check_db_connection():
        # 生产 API 不应以“表面存活、业务接口全部失败”的状态继续运行。
        raise RuntimeError("数据库连接失败，终止后端启动")
    logger.info("✅ 数据库连接检查完成")

    if settings.redis_enabled:
        redis_ok = init_redis(settings.redis_url)
        if redis_ok:
            clear_cache()
            logger.info("✅ Redis 缓存已清空，避免数据版本切换后命中旧值")
            logger.info("✅ Redis 缓存已启用")
        else:
            logger.warning("⚠️ Redis 连接失败，使用内存缓存降级模式")
    else:
        logger.info("ℹ️ Redis 未启用，使用内存缓存")

    cleanup_task = asyncio.create_task(_cache_cleanup_task())

    yield

    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    await engine.dispose()
    logger.info("👋 数据库连接池已关闭")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="""
## AI Commerce Intelligence Platform API v1.7.0

基于10万+电商订单数据，提供数据查询、分析、AI智能查询和数据导出功能。

### 功能模块
- **认证系统**：JWT Token 认证，保护敏感接口
- **订单查询**：订单列表、详情、条件筛选
- **商品与用户**：商品销售排名、用户消费排名
- **数据分析**：销售总览、趋势、热销商品、用户行为、平台分析
- **AI助手**：自然语言查询（Text-to-SQL）
- **数据导出**：CSV/Excel格式导出
- **监控**：实时指标、详细健康检查
- **RFM用户画像**：RFM模型分群、用户价值评估、流失预警

### 安全特性
- JWT Bearer Token 认证
- API 请求频率限制（Rate Limiting）
- SQL 注入防护（LIKE 转义）
- 敏感数据过滤（AI 查询脱敏）
- 响应缓存（热门查询加速）

### 技术栈
FastAPI + SQLAlchemy (async) + MySQL + LangChain + DeepSeek + Pydantic v2
    """,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_SKIP_RATE_LIMIT_PATHS = (
    "/docs", "/redoc", "/health", "/health/detailed", "/health-panel", "/",
    "/demo", "/openapi.json", "/monitor", "/metrics",
)


def _set_security_headers(response) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"


@app.middleware("http")
async def logging_and_rate_limit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable],
):
    start = time.time()

    # CORS 预检请求不占用限流配额，避免浏览器每次跨域调用都被计数
    if request.method == "OPTIONS" or request.url.path in _SKIP_RATE_LIMIT_PATHS:
        response = await call_next(request)
        duration = (time.time() - start) * 1000
        _set_security_headers(response)
        logger.info(f"{request.method} {request.url.path} - {response.status_code} - {duration:.1f}ms")
        return response

    try:
        limit_info = check_rate_limit(
            request,
            trust_proxy_headers=settings.trust_proxy_headers,
        )
    except HTTPException as exc:
        response = JSONResponse(
            status_code=429,
            content={"error_code": "RATE_LIMITED", "message": str(exc.detail)},
            headers=exc.headers,
        )
        _set_security_headers(response)
        return response

    response = await call_next(request)
    duration = (time.time() - start) * 1000

    response.headers["X-RateLimit-Limit"] = str(limit_info["limit"])
    response.headers["X-RateLimit-Remaining"] = str(limit_info["remaining"])
    response.headers["X-RateLimit-Reset"] = str(limit_info["reset"])
    response.headers["X-Response-Time"] = f"{duration:.1f}ms"
    _set_security_headers(response)

    record_request(request.url.path, response.status_code, duration)

    log_level = logging.WARNING if response.status_code >= 400 else logging.INFO
    logger.log(log_level, f"{request.method} {request.url.path} - {response.status_code} - {duration:.1f}ms")
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"未处理异常: {request.method} {request.url.path}")
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_ERROR",
            "message": "服务器内部错误",
            "detail": str(exc) if settings.debug else None,
        },
    )


@app.exception_handler(RfmDataUnavailableError)
async def rfm_unavailable_handler(request: Request, exc: RfmDataUnavailableError):
    """数据库暂无订单数据时返回 503 而非 500，避免错误结果被缓存固化。"""
    logger.info(f"RFM 数据不可用: {exc}")
    return JSONResponse(
        status_code=503,
        content={
            "error_code": "RFM_DATA_UNAVAILABLE",
            "message": str(exc),
        },
    )


app.include_router(auth.router)
app.include_router(orders.router)
app.include_router(products.router)
app.include_router(analytics.router)
app.include_router(ai.router)
app.include_router(export.router)
app.include_router(monitor.router)
app.include_router(rfm.router)


_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
_STATIC_DIR = os.path.join(_DEMO_DIR, "static")


@app.get("/", tags=["系统"])
async def root() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """浏览器默认图标请求无需记录为 404。"""
    return Response(status_code=204)


@app.get("/health", tags=["系统"])
async def health_check() -> dict:
    db_ok = await check_db_connection()
    return {"status": "healthy" if db_ok else "degraded", "database": "connected" if db_ok else "disconnected"}


@app.get("/health/detailed", tags=["系统"])
async def detailed_health_alias() -> dict:
    """兼容运维工具的公开详细健康检查路径。"""
    return await detailed_health()


@app.get("/metrics", response_class=PlainTextResponse, tags=["系统"])
async def metrics_alias() -> PlainTextResponse:
    """公开的 Prometheus 指标路径。"""
    return PlainTextResponse(
        content=(
            render_backend_prometheus()
            + render_rag_prometheus(_load_rag_stats() or {})
        ),
        media_type="text/plain; version=0.0.4",
    )


@app.get("/demo", tags=["体验"])
async def demo_page() -> FileResponse:
    return FileResponse(os.path.join(_DEMO_DIR, "demo.html"))


@app.get("/monitor", tags=["监控"])
async def monitor_page() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "monitor.html"))


@app.get("/health-panel", tags=["监控"])
async def health_panel_page() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "health.html"))
