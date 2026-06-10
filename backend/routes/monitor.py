import logging
import time
import os
import sys
import httpx
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse, JSONResponse

from backend.database import check_db_connection
from backend.routes.auth import get_current_user
from backend.utils.cache import check_redis_health, stats as cache_stats

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor", tags=["监控"])

# 注意：以下统计变量为进程级状态，在多 worker 部署时每个 worker 进程各自独立计数，
# 不会跨进程聚合。如需全局统计，请使用 Redis 或外部存储进行汇总。
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
        "cache": cache_stats(),
    }


@router.get("/health/detailed", summary="详细健康检查")
async def detailed_health():
    checks = {}
    try:
        checks["database"] = {"status": "ok" if await check_db_connection() else "error"}
    except Exception as e:
        checks["database"] = {"status": "error", "detail": str(e)}

    try:
        checks["cache"] = cache_stats()
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


_EXTERNAL_SERVICES = {
    "bi_dashboard": os.getenv("BI_HEALTH_URL", "http://ea-streamlit:8501/BI/_stcore/health"),
    "ai_assistant": os.getenv("AI_HEALTH_URL", "http://ea-ai-assistant:8502/ai/_stcore/health"),
}


@router.get("/services-status", summary="外部服务状态")
async def get_services_status():
    results = {}
    for name, url in _EXTERNAL_SERVICES.items():
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(url)
                results[name] = {"status": "ok" if r.status_code == 200 else "error", "code": r.status_code}
        except Exception as e:
            results[name] = {"status": "error", "detail": str(e)}
    return results


# ─────────────────── RAG 业务知识检索监控 ───────────────────
# ai-ecommerce-assistant 进程内的 Retriever 会把 stats 原子写入 JSON 文件，
# FastAPI 端负责读取与渲染（无 JWT，便于导航页/监控面板调用）。
# 默认路径：ai-ecommerce-assistant/data/rag_stats.json
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RAG_STATS_PATH = os.environ.get(
    "RAG_STATS_PATH",
    str(_PROJECT_ROOT / "ai-ecommerce-assistant" / "data" / "rag_stats.json"),
)


def _load_rag_stats() -> dict | None:
    """从 AI 助手进程写入的 JSON 文件读 stats。"""
    try:
        # 通过 sys.path 引入 metrics 模块（避免重复实现 render）
        ai_root = _PROJECT_ROOT / "ai-ecommerce-assistant"
        if str(ai_root) not in sys.path:
            sys.path.insert(0, str(ai_root))
        from rag import metrics as rag_metrics  # noqa: E402
        return rag_metrics.load_stats(_RAG_STATS_PATH)
    except Exception as e:
        logger.warning("RAG stats 加载失败: %s", e)
        return None


@router.get("/rag-stats", summary="RAG 检索统计")
async def get_rag_stats():
    """RAG 业务知识检索的实时统计（命中率、延迟、TopK 分布、工具调用）。

    数据源：ai-ecommerce-assistant 进程内的 Retriever，每 5 秒落盘一次。
    公开端点，便于监控面板 / 导航页直接调用。
    """
    stats = _load_rag_stats()
    if stats is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "detail": "RAG stats 文件尚未生成（AI 助手可能未启动或未发生检索）",
                "path": _RAG_STATS_PATH,
            },
        )
    return {"status": "ok", "stats": stats}


@router.get("/rag-stats.prom", response_class=PlainTextResponse, summary="RAG 指标 Prometheus 格式")
async def get_rag_stats_prom():
    """把 RAG stats 渲染为 Prometheus exposition 格式，便于 Grafana / 监控系统抓取。"""
    stats = _load_rag_stats()
    try:
        ai_root = _PROJECT_ROOT / "ai-ecommerce-assistant"
        if str(ai_root) not in sys.path:
            sys.path.insert(0, str(ai_root))
        from rag import metrics as rag_metrics  # noqa: E402
        return PlainTextResponse(
            content=rag_metrics.render_prometheus(stats or {}),
            media_type="text/plain; version=0.0.4",
        )
    except Exception as e:
        logger.warning("RAG Prometheus 渲染失败: %s", e)
        return PlainTextResponse(
            content=f"# render error: {e}\n",
            media_type="text/plain; version=0.0.4",
            status_code=500,
        )
