import asyncio
import contextlib
import json
import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database import auth_session_factory, get_auth_db
from backend.models.schemas import AIConversationPopRequest, AIQueryRequest, AIQueryResponse
from backend.services import ai_service
from backend.services.ai_limits import (
    AILimitError,
    acquire_ai_slot,
    ai_execution_slot,
    audit_ai_query,
    usage_fields,
)
from backend.utils.auth import (
    SCOPE_AI_QUERY,
    Principal,
    client_ip,
    record_auth_audit,
    require_scope,
)
from backend.utils.text_cleaner import sanitize_error

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/ai", tags=["AI助手"])


def _model_headers(request: Request) -> dict[str, str | None]:
    return {
        "api_key": request.headers.get("x-llm-api-key"),
        "base_url": request.headers.get("x-llm-base-url"),
        "model": request.headers.get("x-llm-model"),
    }


def _resolve_config(request: Request) -> tuple[ai_service.ModelConfig | None, str]:
    headers = _model_headers(request)
    try:
        config = ai_service.resolve_model_config(**headers)
    except ai_service.AIModelConfigError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "AI_MODEL_CONFIG_INVALID", "message": str(exc)},
        ) from exc
    return config, config.mode if config else settings.llm_mode


def _limit_http_error(exc: AILimitError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error_code": exc.code, "message": exc.message},
    )


async def _audit_denied(
    auth_db: AsyncSession,
    *,
    principal: Principal,
    request: Request,
    query: str,
    mode: str,
    exc: AILimitError,
) -> None:
    await audit_ai_query(
        auth_db,
        request_id=str(uuid4()),
        principal_name=principal.name,
        thread_id="",
        model_mode=mode,
        query=query,
        outcome="denied",
        error_category=exc.code,
    )
    await record_auth_audit(
        auth_db,
        principal_type=principal.principal_type,
        principal_name=principal.name,
        action="ai.query",
        outcome="denied",
        resource="/api/ai/query",
        client=client_ip(request),
        detail=exc.code,
    )


@router.get("/config", summary="获取后端 AI 配置状态")
async def ai_config(
    principal: Principal = Depends(require_scope(SCOPE_AI_QUERY)),
) -> dict:
    """Streamlit 只展示后端状态，不再读取本地模型密钥或初始化本地 Runtime。"""
    mode = settings.llm_mode
    configured = bool(settings.llm_api_key) if mode == "managed" else settings.allow_byok
    return {
        "mode": mode,
        "allow_byok": settings.allow_byok,
        "configured": configured,
        "model": settings.llm_model if mode == "managed" else "",
        "base_url": settings.llm_base_url if mode == "managed" else "",
        "providers": list(settings.llm_allowed_hosts) if mode == "byok" or settings.allow_byok else [],
        "limits": {
            "daily_request_limit": settings.ai_daily_request_limit,
            "max_inflight_per_user": settings.ai_max_inflight_per_user,
            "max_concurrency": settings.ai_max_concurrency,
            "query_timeout_seconds": settings.ai_query_timeout_seconds,
        },
        "rag": ai_service.knowledge_status(),
        "database": await ai_service.database_status(),
        "principal": {"name": principal.name, "role": principal.role},
    }


@router.post("/pop-last-turn", summary="撤销会话末轮")
async def pop_last_turn(
    body: AIConversationPopRequest,
    principal: Principal = Depends(require_scope(SCOPE_AI_QUERY)),
) -> dict:
    """重新生成前撤销末轮，避免旧回答继续作为上下文注入。"""
    removed = await ai_service.pop_last_conversation_turn(
        principal.name,
        body.thread_id,
        body.question,
    )
    return {"removed": removed}


@router.post("/query", response_model=AIQueryResponse, summary="自然语言查询（需认证）")
async def natural_language_query(
    body: AIQueryRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_AI_QUERY)),
    auth_db: AsyncSession = Depends(get_auth_db),
):
    """非流式 Text-to-SQL。额度、并发与审计全部在后端执行。"""
    config, mode = _resolve_config(request)
    started = time.perf_counter()
    audit_request_id = str(uuid4())
    try:
        async with ai_execution_slot(principal.name, auth_db):
            response = await asyncio.wait_for(
                ai_service.process_natural_language_query(
                    body.query,
                    body.thread_id,
                    owner=principal.name,
                    config=config,
                ),
                timeout=settings.ai_query_timeout_seconds,
            )
    except AILimitError as exc:
        await _audit_denied(
            auth_db,
            principal=principal,
            request=request,
            query=body.query,
            mode=mode,
            exc=exc,
        )
        raise _limit_http_error(exc) from exc
    except (TimeoutError, asyncio.TimeoutError) as exc:
        await audit_ai_query(
            auth_db,
            request_id=audit_request_id,
            principal_name=principal.name,
            thread_id=body.thread_id or "",
            model_mode=mode,
            query=body.query,
            outcome="timeout",
            latency_ms=round((time.perf_counter() - started) * 1000),
            error_category="TimeoutError",
        )
        raise HTTPException(
            status_code=504,
            detail={"error_code": "AI_QUERY_TIMEOUT", "message": "AI 查询超时，请缩小问题范围后重试"},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 模型/数据库异常已由 Runtime 降级，这里兜底
        logger.warning("AI 查询失败: %s", sanitize_error(exc))
        await audit_ai_query(
            auth_db,
            request_id=audit_request_id,
            principal_name=principal.name,
            thread_id=body.thread_id or "",
            model_mode=mode,
            query=body.query,
            outcome="error",
            latency_ms=round((time.perf_counter() - started) * 1000),
            error_category=type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail={"error_code": "AI_QUERY_FAILED", "message": "AI 查询失败，请稍后重试"},
        ) from exc

    usage = usage_fields(response)
    await audit_ai_query(
        auth_db,
        request_id=response.request_id or audit_request_id,
        principal_name=principal.name,
        thread_id=response.thread_id or body.thread_id or "",
        model_mode=mode,
        query=body.query,
        outcome="success",
        intent=response.intent or "",
        **usage,
    )
    return response


def _event(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"


def _stream_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ai_service.AIModelConfigError):
        return {"type": "error", "error_code": "AI_MODEL_CONFIG_INVALID", "message": str(exc)}
    if isinstance(exc, AILimitError):
        return {"type": "error", "error_code": exc.code, "message": exc.message}
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return {"type": "error", "error_code": "AI_QUERY_TIMEOUT", "message": "AI 查询超时"}
    return {"type": "error", "error_code": "AI_STREAM_FAILED", "message": "AI 查询失败，请稍后重试"}


@router.post("/stream", summary="自然语言查询（NDJSON 流式）")
async def stream_natural_language_query(
    body: AIQueryRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_AI_QUERY)),
    auth_db: AsyncSession = Depends(get_auth_db),
):
    """NDJSON 事件流：start / step / token / final / error。

    Streamlit AI 页面统一调用此入口；Runtime、RAG、只读 SQL、额度、并发
    与审计都在后端完成，浏览器只负责渲染。
    """
    config, mode = _resolve_config(request)
    try:
        slot = await acquire_ai_slot(principal.name, auth_db)
    except AILimitError as exc:
        await _audit_denied(
            auth_db,
            principal=principal,
            request=request,
            query=body.query,
            mode=mode,
            exc=exc,
        )
        raise _limit_http_error(exc) from exc

    thread_id = body.thread_id or str(uuid4())
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    audit_request_id = str(uuid4())

    async def _write_audit(outcome: str, response: AIQueryResponse | None, error_category: str) -> None:
        async with auth_session_factory() as session:
            usage = usage_fields(response) if response is not None else {}
            await audit_ai_query(
                session,
                request_id=(response.request_id if response else None) or audit_request_id,
                principal_name=principal.name,
                thread_id=(response.thread_id if response else None) or thread_id,
                model_mode=mode,
                query=body.query,
                outcome=outcome,
                intent=(response.intent if response else "") or "",
                error_category=error_category,
                **usage,
            )

    async def _producer() -> None:
        response: AIQueryResponse | None = None
        try:
            queue.put_nowait({"type": "start", "thread_id": thread_id, "mode": mode})
            response = await asyncio.wait_for(
                ai_service.stream_natural_language_query(
                    body.query,
                    thread_id,
                    owner=principal.name,
                    config=config,
                    on_step=lambda step: queue.put_nowait({"type": "step", "step": step}),
                    on_token=lambda delta: queue.put_nowait({"type": "token", "delta": delta}),
                ),
                timeout=settings.ai_query_timeout_seconds,
            )
            queue.put_nowait({"type": "final", "response": response.model_dump(mode="json")})
            await _write_audit("success", response, "")
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await _write_audit("cancelled", response, "CancelledError")
            raise
        except (TimeoutError, asyncio.TimeoutError):
            queue.put_nowait(_stream_error(TimeoutError()))
            await _write_audit("timeout", response, "TimeoutError")
        except Exception as exc:  # noqa: BLE001 - 流内错误必须以事件形式返回
            logger.warning("AI 流式查询失败: %s", sanitize_error(exc))
            queue.put_nowait(_stream_error(exc))
            await _write_audit("error", response, type(exc).__name__)
        finally:
            slot.release()
            queue.put_nowait(None)

    task = asyncio.create_task(_producer())

    async def _events():
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield _event(item)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    return StreamingResponse(
        _events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/test", summary="测试当前模型连接")
async def test_model_connection(
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_AI_QUERY)),
    auth_db: AsyncSession = Depends(get_auth_db),
) -> dict:
    """连接测试同样走后端策略；密钥只存在于请求内存，不写入日志与审计。"""
    config, mode = _resolve_config(request)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "MODEL_NOT_CONFIGURED", "message": "平台模型尚未配置"},
        )
    ok = await ai_service.test_model_connection(config)
    await record_auth_audit(
        auth_db,
        principal_type=principal.principal_type,
        principal_name=principal.name,
        action="ai.test",
        outcome="success" if ok else "error",
        resource="/api/ai/test",
        client=client_ip(request),
        detail=mode,
    )
    if not ok:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "MODEL_UNREACHABLE", "message": "模型连接失败"},
        )
    return {"ok": True, "mode": mode, "model": config.model}
