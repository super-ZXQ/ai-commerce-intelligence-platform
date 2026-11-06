"""业务数据导入接口：上传、异步校验、状态查询与预览。

所有入口都在服务端校验 data:import，不能只靠前端隐藏按钮；文件名、大小、
扩展名与魔数在上传阶段就拒绝，避免把不可信文件交给解析器。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database import get_import_db
from backend.models.database_models import DataImportRun
from backend.models.import_schemas import (
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportExecuteRequest,
    ImportPreviewResponse,
    ImportResultResponse,
    ImportRowPage,
    ImportRunStatus,
)
from backend.services import data_import_service as service
from backend.utils.auth import (
    SCOPE_ADMIN_MANAGE,
    SCOPE_DATA_IMPORT,
    SCOPE_DATA_PII,
    Principal,
    require_scope,
)
from backend.utils.text_cleaner import sanitize_error

router = APIRouter(prefix="/api/data-import", tags=["数据导入"])
settings = get_settings()
logger = logging.getLogger(__name__)

_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _raise(exc: service.ImportFileError) -> NoReturn:
    """把服务层的可回显错误转换成统一 detail 结构。"""
    raise HTTPException(
        status_code=exc.http_status,
        detail={"error_code": exc.code, "message": exc.message, **exc.extra},
    )


@router.post(
    "/upload",
    response_model=ImportRunStatus,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传订单文件并发起异步校验",
)
async def upload_import_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    conflict_policy: Literal["reject", "skip"] = Form("reject"),
    db: AsyncSession = Depends(get_import_db),
    principal: Principal = Depends(require_scope(SCOPE_DATA_IMPORT)),
):
    """接收 CSV/XLSX，立即返回运行 ID；校验在后台完成，前端轮询运行状态。"""
    original = Path(file.filename or "").name.strip()
    if not original:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "IMPORT_MISSING_FILENAME", "message": "缺少文件名"},
        )
    if len(original) > 255 or any(ord(char) < 32 for char in original):
        raise HTTPException(
            status_code=422,
            detail={"error_code": "IMPORT_INVALID_FILENAME", "message": "文件名包含非法字符或过长"},
        )

    first_chunk = await file.read(_UPLOAD_CHUNK_BYTES)
    if not first_chunk:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "IMPORT_EMPTY_FILE", "message": "文件为空"},
        )

    try:
        source_type = service.detect_source_type(original, first_chunk)
    except service.ImportFileError as exc:
        _raise(exc)

    temp_path = service.create_temp_upload(Path(original).suffix.lower())
    digest = hashlib.sha256()
    written = 0
    try:
        with temp_path.open("wb") as output:
            chunk = first_chunk
            while chunk:
                written += len(chunk)
                if written > settings.data_import_max_file_bytes:
                    raise service.ImportFileError(
                        "IMPORT_FILE_TOO_LARGE",
                        f"文件超过上限 {settings.data_import_max_file_bytes} 字节",
                        http_status=413,
                    )
                digest.update(chunk)
                output.write(chunk)
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if written == 0:
            raise service.ImportFileError("IMPORT_EMPTY_FILE", "文件为空")

        headers = await asyncio.to_thread(service.read_headers, temp_path, source_type)
        mapping = service.build_column_mapping(headers)
        options = {"conflict_policy": conflict_policy, **mapping}

        run = DataImportRun(
            source=original,
            source_sha256=digest.hexdigest(),
            mode="upload",
            status=service.RUN_VALIDATING,
            operator=principal.name,
            rows_read=0,
            started_at=_utc_now_naive(),
            source_type=source_type,
            original_filename=original,
            source_size_bytes=written,
            options_json=options,
            conflict_policy=conflict_policy,
            heartbeat_at=_utc_now_naive(),
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
    except service.ImportFileError as exc:
        temp_path.unlink(missing_ok=True)
        _raise(exc)
    except HTTPException:
        temp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 - 不把驱动异常/文件路径回显给调用方
        temp_path.unlink(missing_ok=True)
        logger.exception("创建导入任务失败: %s", sanitize_error(exc))
        raise HTTPException(
            status_code=503,
            detail={"error_code": "IMPORT_UPLOAD_FAILED", "message": "上传失败，请稍后重试"},
        ) from exc

    background_tasks.add_task(service.validate_import_run, run.id, str(temp_path))
    return service.build_run_status(run)


@router.get("/runs/{run_id}", response_model=ImportRunStatus, summary="查询导入运行状态")
async def get_import_run(
    run_id: int,
    db: AsyncSession = Depends(get_import_db),
    principal: Principal = Depends(require_scope(SCOPE_DATA_IMPORT)),
):
    try:
        run = await service.load_run(db, run_id)
        service.assert_run_owner(
            run, principal.name, principal.has_scope(SCOPE_ADMIN_MANAGE)
        )
    except service.ImportFileError as exc:
        _raise(exc)
    return service.build_run_status(run)


@router.get(
    "/runs/{run_id}/preview",
    response_model=ImportPreviewResponse,
    summary="导入预览：列映射、计数与三类明细样本",
)
async def preview_import_run(
    run_id: int,
    db: AsyncSession = Depends(get_import_db),
    principal: Principal = Depends(require_scope(SCOPE_DATA_IMPORT)),
):
    try:
        run = await service.load_run(db, run_id)
        service.assert_run_owner(
            run, principal.name, principal.has_scope(SCOPE_ADMIN_MANAGE)
        )
        can_view_pii = principal.has_scope(SCOPE_DATA_PII)
        return await service.build_preview(db, run, can_view_pii=can_view_pii)
    except service.ImportFileError as exc:
        _raise(exc)


@router.get(
    "/runs/{run_id}/rows",
    response_model=ImportRowPage,
    summary="导入明细分页；单页最多 100 行",
)
async def list_import_rows(
    run_id: int,
    row_status: str = Query("valid", alias="status", description="valid/conflict/invalid/skipped/imported/failed"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1),
    db: AsyncSession = Depends(get_import_db),
    principal: Principal = Depends(require_scope(SCOPE_DATA_IMPORT)),
):
    try:
        run = await service.load_run(db, run_id)
        service.assert_run_owner(
            run, principal.name, principal.has_scope(SCOPE_ADMIN_MANAGE)
        )
        return await service.build_row_page(
            db,
            run,
            row_status=row_status,
            page=page,
            page_size=page_size,
            can_view_pii=principal.has_scope(SCOPE_DATA_PII),
        )
    except service.ImportFileError as exc:
        _raise(exc)


@router.post(
    "/runs/{run_id}/confirm",
    response_model=ImportConfirmResponse,
    summary="二次确认：核对文件摘要并领取执行令牌",
)
async def confirm_import_run(
    run_id: int,
    payload: ImportConfirmRequest,
    db: AsyncSession = Depends(get_import_db),
    principal: Principal = Depends(require_scope(SCOPE_DATA_IMPORT)),
):
    """确认操作与执行分离：确认阶段只校验摘要并签发令牌，执行必须再带令牌。

    重复确认会轮换令牌，便于客户端在超时后重试；已进入 running/completed 时
    不再签发新令牌。
    """
    try:
        run, token = await service.confirm_run(
            db,
            run_id,
            payload.expected_sha256,
            principal.name,
            is_admin=principal.has_scope(SCOPE_ADMIN_MANAGE),
        )
    except service.ImportFileError as exc:
        _raise(exc)
    return ImportConfirmResponse(run=service.build_run_status(run), confirm_token=token)


@router.post(
    "/runs/{run_id}/execute",
    response_model=ImportRunStatus,
    status_code=status.HTTP_202_ACCEPTED,
    summary="执行导入：原子抢占并后台分块入库",
)
async def execute_import_run(
    run_id: int,
    payload: ImportExecuteRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_import_db),
    principal: Principal = Depends(require_scope(SCOPE_DATA_IMPORT)),
):
    """幂等执行入口：已 running/completed 直接返回当前状态，不重复调度。

    真正的互斥由执行任务持有的 MySQL GET_LOCK 保证；这里提前探测，冲突时返回 409。
    """
    try:
        run, should_schedule = await service.request_execution(
            db,
            run_id,
            payload.confirm_token,
            principal.name,
            is_admin=principal.has_scope(SCOPE_ADMIN_MANAGE),
        )
    except service.ImportFileError as exc:
        _raise(exc)
    if should_schedule:
        # 执行时间可能远超前台请求；放到后台任务里，避免阻塞响应。
        background_tasks.add_task(service.execute_import_run, run.id)
    return service.build_run_status(run)


@router.get(
    "/runs/{run_id}/result",
    response_model=ImportResultResponse,
    summary="执行结果：计数、失败明细与跳过数",
)
async def get_import_result(
    run_id: int,
    db: AsyncSession = Depends(get_import_db),
    principal: Principal = Depends(require_scope(SCOPE_DATA_IMPORT)),
):
    try:
        run = await service.load_run(db, run_id)
        service.assert_run_owner(
            run, principal.name, principal.has_scope(SCOPE_ADMIN_MANAGE)
        )
        return await service.build_execution_result(
            db, run, can_view_pii=principal.has_scope(SCOPE_DATA_PII)
        )
    except service.ImportFileError as exc:
        _raise(exc)


@router.get(
    "/runs/{run_id}/failures.csv",
    summary="导出失败明细 CSV（UTF-8 BOM，防公式注入）",
)
async def export_import_failures(
    run_id: int,
    db: AsyncSession = Depends(get_import_db),
    principal: Principal = Depends(require_scope(SCOPE_DATA_IMPORT)),
):
    try:
        run = await service.load_run(db, run_id)
        service.assert_run_owner(
            run, principal.name, principal.has_scope(SCOPE_ADMIN_MANAGE)
        )
        content, truncated = await service.export_failure_csv(db, run)
    except service.ImportFileError as exc:
        _raise(exc)
    headers = {"Content-Disposition": 'attachment; filename="import_failures.csv"'}
    if truncated:
        # 截断必须让客户端可见，避免误以为拿到了全部失败行。
        headers["X-Result-Truncated"] = "true"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )
