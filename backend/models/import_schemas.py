"""业务数据导入接口的传输模型。

只暴露校验/预览需要的字段；文件内容、行级明细与运行状态都在服务端持久化，
前端不能依赖客户端隐藏来获得权限。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ImportRowStatus = Literal["valid", "conflict", "invalid", "skipped", "imported", "failed"]


class ImportCounts(BaseModel):
    """运行级计数；校验阶段先填 valid/conflict/invalid，执行阶段再填 imported/failed。"""

    rows_read: int = 0
    rows_valid: int = 0
    rows_conflict: int = 0
    rows_invalid: int = 0
    rows_skipped: int = 0
    rows_rejected: int = 0
    rows_inserted: int = 0
    rows_refunded: int = 0


class ImportRunStatus(BaseModel):
    """运行状态查询与上传响应共用。"""

    run_id: int
    status: str
    operator: str
    source_type: str | None = None
    original_filename: str | None = None
    size_bytes: int = 0
    sha256: str = ""
    conflict_policy: str = "reject"
    counts: ImportCounts = Field(default_factory=ImportCounts)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    error_code: str = ""
    error_message: str = ""
    can_confirm: bool = False
    can_execute: bool = False
    progress_percent: float = 0.0


class ImportColumnMapping(BaseModel):
    """一列源数据到标准字段的映射；source 为空表示文件没有提供。"""

    canonical: str
    source: str | None = None
    required: bool = False


class ImportRowItem(BaseModel):
    row_no: int
    status: str
    order_id: str = ""
    error_code: str = ""
    error_message: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)


class ImportRowPage(BaseModel):
    run_id: int
    status: str
    page: int
    page_size: int
    total: int
    total_pages: int
    items: list[ImportRowItem]


class ImportPreviewResponse(BaseModel):
    run: ImportRunStatus
    columns: list[ImportColumnMapping] = Field(default_factory=list)
    unknown_columns: list[str] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)
    preview_limit: int = 100
    valid: list[ImportRowItem] = Field(default_factory=list)
    conflict: list[ImportRowItem] = Field(default_factory=list)
    invalid: list[ImportRowItem] = Field(default_factory=list)
