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
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database import get_import_db
from backend.models.database_models import DataImportRun
from backend.models.import_schemas import ImportPreviewResponse, ImportRowPage, ImportRunStatus
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


def _raise(exc: service.ImportFileError) -> None:
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
