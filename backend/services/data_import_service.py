"""业务数据导入：文件识别、流式解析、行级校验、暂存与预览。

设计约束：
- CSV 使用 csv.reader 逐行读取，XLSX 使用 openpyxl read_only，禁止 pandas 全量载入；
- 校验阶段把所有明细写入 data_import_rows，后续确认/执行只读暂存行，不再重新解析文件；
- 运行状态、计数、错误码都落库，进程重启后仍可查询；
- 原始文件只落在临时目录，校验结束立即删除，不覆盖 data/cleaned_orders.csv。
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import logging
import os
import secrets
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import load_workbook
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database import import_session_factory
from backend.models.database_models import DataImportRow, DataImportRun, Order, OrderEvent
from backend.models.import_schemas import (
    ImportColumnMapping,
    ImportCounts,
    ImportPreviewResponse,
    ImportResultResponse,
    ImportRowItem,
    ImportRowPage,
    ImportRunListResponse,
    ImportRunStatus,
)
from backend.services.rfm_service import clear_rfm_snapshot_cache
from backend.utils.cache import invalidate_tags
from backend.utils.pii import mask_user_name
from backend.utils.text_cleaner import sanitize_error

logger = logging.getLogger(__name__)
settings = get_settings()

_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
_READ_CHUNK_BYTES = 1024 * 1024

RUN_VALIDATING = "validating"
RUN_VALIDATED = "validated"
RUN_CONFIRMED = "confirmed"
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

# 活跃任务不参与过期清理：校验/执行中的记录可能仍被后台任务引用。
_ACTIVE_STATUSES = (RUN_VALIDATING, RUN_RUNNING)
_CLEANUP_BATCH_SIZE = 200

ROW_VALID = "valid"
ROW_CONFLICT = "conflict"
ROW_INVALID = "invalid"
ROW_SKIPPED = "skipped"
ROW_IMPORTED = "imported"
ROW_FAILED = "failed"

_PREVIEW_STATUSES = (ROW_VALID, ROW_CONFLICT, ROW_INVALID)
_ROW_PAGE_STATUSES = frozenset(
    {ROW_VALID, ROW_CONFLICT, ROW_INVALID, ROW_SKIPPED, ROW_IMPORTED, ROW_FAILED}
)
_FAILED_EXPORT_STATUSES = (ROW_INVALID, ROW_FAILED)

# 导入互斥锁必须是稳定字符串：同一实例的多个进程/任务共用一把锁。
_IMPORT_LOCK_NAME = ("ea_data_import_" + settings.db_name)[:64]
# 确定性 UUIDv5 命名空间：同一文件、同一行、同一事件类型重放时得到同一 event_id。
_IMPORT_EVENT_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "ai-commerce-intelligence-platform/data-import"
)

CONFLICT_POLICIES = ("reject", "skip")
# 冲突策略名沿用接口层默认：reject = 该行失败并继续；skip = 跳过冲突行。
CONFLICT_POLICY_REJECT = "reject"
CONFLICT_POLICY_SKIP = "skip"

_MONEY_QUANT = Decimal("0.01")


class ImportFileError(Exception):
    """可安全回显给调用方的文件/校验错误。"""

    def __init__(self, code: str, message: str, *, http_status: int = 422, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.extra = extra


@dataclass(frozen=True)
class CanonicalField:
    name: str
    aliases: tuple[str, ...]
    required: bool


# 顺序与 data/cleaned_orders.csv 一致；中文是业务同事直接上传时的默认表头。
CANONICAL_FIELDS: tuple[CanonicalField, ...] = (
    CanonicalField("order_seq_id", ("订单顺序编号", "order_seq_id"), False),
    CanonicalField("order_id", ("订单号", "order_id"), True),
    # 用户名与商品编号虽非订单主键，但 BI/RFM 依赖二者做分层与商品分析：
    # 缺列时整批数据都无法进入分析口径，因此在表头阶段就拒绝。
    CanonicalField("user_name", ("用户名", "user_name"), True),
    CanonicalField("product_id", ("商品编号", "product_id"), True),
    CanonicalField("order_amount", ("订单金额", "order_amount"), True),
    CanonicalField("payment_amount", ("付款金额", "payment_amount"), True),
    CanonicalField("channel_id", ("渠道编号", "channel_id"), False),
    CanonicalField("platform_type", ("平台类型", "platform_type"), True),
    CanonicalField("order_time", ("下单时间", "order_time"), True),
    CanonicalField("payment_time", ("付款时间", "payment_time"), False),
    CanonicalField("is_refund", ("是否退款", "is_refund"), False),
    CanonicalField("discount_amount", ("优惠金额", "discount_amount"), False),
    CanonicalField("payment_duration_sec", ("支付耗时_秒", "payment_duration_sec"), False),
    CanonicalField("order_date", ("下单日期", "order_date"), False),
    CanonicalField("order_hour", ("下单小时", "order_hour"), False),
    CanonicalField("weekday", ("星期几", "weekday"), False),
)

_REQUIRED_FIELDS = tuple(field.name for field in CANONICAL_FIELDS if field.required)


def _normalize_header(value: str) -> str:
    """表头归一化：去 BOM/空白并转小写，允许“订单号”和“order_id”两种写法。"""
    return "".join(str(value).replace("\ufeff", "").split()).lower()


_ALIAS_INDEX: dict[str, str] = {
    _normalize_header(alias): field.name
    for field in CANONICAL_FIELDS
    for alias in field.aliases
}


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _cell_text(value: Any) -> str:
    """把 CSV 字符串或 Excel 单元格统一成可校验的文本，不做业务解释。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value).strip()


def detect_source_type(filename: str, head: bytes) -> str:
    """扩展名 + 魔数双重校验：拒绝把任意压缩包/二进制伪装成 CSV/XLSX。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in (".csv", ".xlsx"):
        raise ImportFileError(
            "IMPORT_UNSUPPORTED_FILE_TYPE",
            "仅支持 .csv 或 .xlsx 文件",
            http_status=415,
        )
    if suffix == ".xlsx":
        if not head.startswith(b"PK\x03\x04"):
            raise ImportFileError(
                "IMPORT_FILE_TYPE_MISMATCH",
                "文件内容不是有效的 XLSX（缺少 ZIP 魔数）",
            )
        return "xlsx"
    # CSV 没有标准魔数；至少要排除常见二进制容器与 NUL 字节。
    if head.startswith((b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"\x7fELF")):
        raise ImportFileError(
            "IMPORT_FILE_TYPE_MISMATCH",
            "文件内容不是文本 CSV，请另存为 UTF-8 CSV",
        )
    if b"\x00" in head:
        raise ImportFileError(
            "IMPORT_FILE_TYPE_MISMATCH",
            "文件内容包含二进制字节，请另存为 UTF-8 CSV",
        )
    return "csv"


def _validate_xlsx_archive(path: Path) -> None:
    """openpyxl 只读模式仍会解压；先限制条目数量与解压总量，避免 zip 炸弹。"""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not any(info.filename == "xl/workbook.xml" for info in infos):
                raise ImportFileError(
                    "IMPORT_FILE_TYPE_MISMATCH",
                    "XLSX 缺少 workbook.xml，文件可能已损坏",
                )
            if len(infos) > 10000:
                raise ImportFileError("IMPORT_XLSX_TOO_COMPLEX", "XLSX 内部条目过多")
            total = sum(info.file_size for info in infos)
            if total > settings.data_import_max_file_bytes * 20:
                raise ImportFileError(
                    "IMPORT_XLSX_TOO_LARGE",
                    "XLSX 解压后体积超过限制",
                    http_status=413,
                )
    except zipfile.BadZipFile as exc:
        raise ImportFileError("IMPORT_FILE_TYPE_MISMATCH", "XLSX 压缩结构无效") from exc


def read_headers(path: Path, source_type: str) -> list[str]:
    """只读第一行/首个工作表表头；用于上传时快速拒绝列缺失，不触碰数据行。"""
    if source_type == "xlsx":
        _validate_xlsx_archive(path)
        workbook = None
        try:
            workbook = load_workbook(filename=path, read_only=True, data_only=True)
            if not workbook.worksheets:
                raise ImportFileError("IMPORT_EMPTY_FILE", "XLSX 没有工作表")
            first_row = next(
                workbook.worksheets[0].iter_rows(min_row=1, max_row=1, values_only=True),
                None,
            )
        finally:
            if workbook is not None:
                workbook.close()
        return [_cell_text(value) for value in (first_row or ())]
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            return next(csv.reader(file), [])
    except UnicodeDecodeError as exc:
        raise ImportFileError(
            "IMPORT_ENCODING_UNSUPPORTED",
            "CSV 不是 UTF-8 编码，请另存为 UTF-8（可带 BOM）后重试",
        ) from exc


def build_column_mapping(headers: list[str]) -> dict[str, Any]:
    """把源表头映射到标准字段，并返回预览所需的列信息。"""
    if not headers:
        raise ImportFileError("IMPORT_EMPTY_FILE", "文件缺少表头")
    if len(headers) > settings.data_import_max_columns:
        raise ImportFileError(
            "IMPORT_TOO_MANY_COLUMNS",
            f"列数超过上限 {settings.data_import_max_columns}",
        )

    seen_headers: set[str] = set()
    canonical_sources: dict[str, str] = {}
    unknown: list[str] = []
    for header in headers:
        header = header.strip()
        if not header:
            continue
        if header in seen_headers:
            raise ImportFileError(
                "IMPORT_DUPLICATE_COLUMN",
                f"表头重复：{header}",
                duplicate_columns=[header],
            )
        seen_headers.add(header)
        canonical = _ALIAS_INDEX.get(_normalize_header(header))
        if canonical is None:
            unknown.append(header)
            continue
        if canonical in canonical_sources:
            raise ImportFileError(
                "IMPORT_DUPLICATE_COLUMN",
                f"多个列映射到同一字段：{canonical}",
                duplicate_columns=[header, canonical_sources[canonical]],
            )
        canonical_sources[canonical] = header

    missing = [name for name in _REQUIRED_FIELDS if name not in canonical_sources]
    if missing:
        raise ImportFileError(
            "IMPORT_MISSING_COLUMNS",
            f"缺少必填列：{', '.join(missing)}",
            missing_columns=missing,
            unknown_columns=unknown,
        )

    columns = [
        {
            "canonical": field.name,
            "source": canonical_sources.get(field.name),
            "required": field.required,
        }
        for field in CANONICAL_FIELDS
    ]
    return {
        "columns": columns,
        "unknown_columns": unknown,
        "missing_required": missing,
        "source_columns": canonical_sources,
    }


def _parse_datetime(
    value: str, label: str, errors: list[tuple[str, str]], *, required: bool
) -> datetime | None:
    raw = value.strip()
    if not raw:
        if required:
            errors.append(("REQUIRED_FIELD", f"{label}不能为空"))
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        errors.append(("INVALID_DATETIME", f"{label}格式应为 YYYY-MM-DD HH:MM:SS"))
        return None
    if parsed.tzinfo is not None:
        # 业务时间是 Asia/Shanghai 墙钟；带时区输入先换算再落库。
        parsed = parsed.astimezone(_BUSINESS_TZ).replace(tzinfo=None)
    return parsed


def _parse_money(
    value: str, label: str, errors: list[tuple[str, str]], *, required: bool
) -> str | None:
    raw = value.strip().replace(",", "")
    if not raw:
        if required:
            errors.append(("REQUIRED_FIELD", f"{label}不能为空"))
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        errors.append(("INVALID_AMOUNT", f"{label}不是有效数字"))
        return None
    if not amount.is_finite() or amount < 0:
        errors.append(("INVALID_AMOUNT", f"{label}必须是非负数字"))
        return None
    if amount.adjusted() > 16:
        errors.append(("AMOUNT_OUT_OF_RANGE", f"{label}超出可存储范围"))
        return None
    return str(amount.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP))


def _parse_int(value: str, label: str, errors: list[tuple[str, str]]) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation:
        errors.append(("INVALID_INTEGER", f"{label}必须是整数"))
        return None
    if not number.is_finite() or number != number.to_integral_value():
        errors.append(("INVALID_INTEGER", f"{label}必须是整数"))
        return None
    return int(number)


def _parse_refund(value: str, errors: list[tuple[str, str]]) -> str:
    raw = value.strip().lower()
    if raw in ("", "否", "false", "0", "n", "no"):
        return "否"
    if raw in ("是", "true", "1", "y", "yes"):
        return "是"
    errors.append(("INVALID_IS_REFUND", "是否退款只能填写是/否"))
    return "否"


def _clean_text(value: str, label: str, max_length: int, errors: list[tuple[str, str]]) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    if len(raw) > max_length:
        errors.append(("FIELD_TOO_LONG", f"{label}超过 {max_length} 个字符"))
        return None
    if any(ord(char) < 32 for char in raw):
        errors.append(("INVALID_TEXT", f"{label}包含不可见控制字符"))
        return None
    return raw


def normalize_row(raw: dict[str, str], sources: dict[str, str]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """按标准字段校验单行；返回可 JSON 序列化的规范值。"""
    errors: list[tuple[str, str]] = []

    def value_of(canonical: str) -> str:
        source = sources.get(canonical)
        return raw.get(source, "") if source else ""

    order_id = _clean_text(value_of("order_id"), "订单号", 50, errors)
    if not order_id:
        errors.append(("REQUIRED_FIELD", "订单号不能为空"))

    order_seq_id = _parse_int(value_of("order_seq_id"), "订单顺序编号", errors)
    if order_seq_id is not None and order_seq_id <= 0:
        errors.append(("INVALID_INTEGER", "订单顺序编号必须大于 0"))
        order_seq_id = None

    user_name = _clean_text(value_of("user_name"), "用户名", 100, errors)
    if not user_name:
        errors.append(("REQUIRED_FIELD", "用户名不能为空"))
    product_id = _clean_text(value_of("product_id"), "商品编号", 50, errors)
    if not product_id:
        errors.append(("REQUIRED_FIELD", "商品编号不能为空"))
    channel_id = _clean_text(value_of("channel_id"), "渠道编号", 50, errors)
    platform_type = _clean_text(value_of("platform_type"), "平台类型", 50, errors)
    if not platform_type:
        errors.append(("REQUIRED_FIELD", "平台类型不能为空"))

    order_amount = _parse_money(value_of("order_amount"), "订单金额", errors, required=True)
    payment_amount = _parse_money(value_of("payment_amount"), "付款金额", errors, required=True)
    if order_amount is not None and Decimal(order_amount) <= 0:
        errors.append(("AMOUNT_NOT_POSITIVE", "订单金额必须大于 0"))
    if payment_amount is not None and Decimal(payment_amount) <= 0:
        errors.append(("AMOUNT_NOT_POSITIVE", "付款金额必须大于 0"))
    if order_amount is not None and payment_amount is not None and Decimal(payment_amount) > Decimal(order_amount):
        errors.append(("PAYMENT_EXCEEDS_ORDER", "付款金额不能大于订单金额"))
    discount_amount = _parse_money(value_of("discount_amount"), "优惠金额", errors, required=False) or "0.00"

    order_time = _parse_datetime(value_of("order_time"), "下单时间", errors, required=True)
    payment_time = _parse_datetime(value_of("payment_time"), "付款时间", errors, required=False)
    if order_time is not None and payment_time is None:
        payment_time = order_time
    if order_time is not None and payment_time is not None and payment_time < order_time:
        errors.append(("PAYMENT_BEFORE_ORDER", "付款时间不能早于下单时间"))

    duration = _parse_int(value_of("payment_duration_sec"), "支付耗时_秒", errors)
    if duration is not None and duration < 0:
        errors.append(("INVALID_INTEGER", "支付耗时_秒必须是非负整数"))
        duration = None
    if duration is None and order_time is not None and payment_time is not None:
        duration = max(int((payment_time - order_time).total_seconds()), 0)

    refund = _parse_refund(value_of("is_refund"), errors)

    # 下单日期/小时/星期几一律由下单时间推导，避免源文件与事实时间不一致。
    order_date = order_time.date().isoformat() if order_time else None
    order_hour = order_time.hour if order_time else None
    weekday = order_time.strftime("%A") if order_time else None

    normalized: dict[str, Any] = {
        "order_seq_id": order_seq_id,
        "order_id": order_id or "",
        "user_name": user_name,
        "product_id": product_id,
        "order_amount": order_amount,
        "payment_amount": payment_amount,
        "channel_id": channel_id,
        "platform_type": platform_type,
        "order_time": order_time.isoformat(sep=" ") if order_time else None,
        "payment_time": payment_time.isoformat(sep=" ") if payment_time else None,
        "is_refund": refund,
        "discount_amount": discount_amount,
        "payment_duration_sec": duration,
        "order_date": order_date,
        "order_hour": order_hour,
        "weekday": weekday,
    }
    return normalized, errors


@dataclass
class StagedRow:
    row_no: int
    order_id: str
    status: str
    normalized: dict[str, Any] | None
    raw: dict[str, str] | None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class _SeenIds:
    order_ids: dict[str, int]
    seq_ids: dict[int, int]


def _iter_source_rows(path: Path, source_type: str, headers: list[str]):
    """逐行产出 (row_no, cells)；CSV/XLSX 都保持流式，不把整表读进内存。"""
    if source_type == "xlsx":
        workbook = load_workbook(filename=path, read_only=True, data_only=True)
        try:
            if not workbook.worksheets:
                raise ImportFileError("IMPORT_EMPTY_FILE", "XLSX 没有工作表")
            for row_no, values in enumerate(
                workbook.worksheets[0].iter_rows(min_row=2, values_only=True), start=2
            ):
                yield row_no, [_cell_text(value) for value in (values or ())]
        finally:
            workbook.close()
        return

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.reader(file)
            next(reader, None)  # 跳过表头
            for row_no, values in enumerate(reader, start=2):
                yield row_no, [_cell_text(value) for value in values]
    except UnicodeDecodeError as exc:
        raise ImportFileError(
            "IMPORT_ENCODING_UNSUPPORTED",
            "CSV 不是 UTF-8 编码，请另存为 UTF-8（可带 BOM）后重试",
        ) from exc
    except csv.Error as exc:
        raise ImportFileError("IMPORT_CSV_PARSE_ERROR", f"CSV 解析失败：{exc}") from exc


def _is_blank(cells: list[str]) -> bool:
    return all(not cell.strip() for cell in cells)


def _build_raw(headers: list[str], cells: list[str]) -> dict[str, str]:
    """原始行只保存有表头的列，并按单元格上限截断，避免暂存表被超长文本撑爆。"""
    raw: dict[str, str] = {}
    for index, header in enumerate(headers):
        if not header.strip():
            continue
        cell = cells[index] if index < len(cells) else ""
        raw[header] = cell[: settings.data_import_max_cell_chars]
    return raw


def _first_error(errors: list[tuple[str, str]]) -> tuple[str, str]:
    messages = [message for _, message in errors[:3]]
    return errors[0][0], "；".join(messages)[:500]


async def _mark_run_failed(db: AsyncSession, run_id: int, code: str, message: str) -> None:
    """校验失败时清空已提交的暂存行，避免半成品继续被预览或执行。"""
    try:
        await db.execute(delete(DataImportRow).where(DataImportRow.run_id == run_id))
        await db.execute(
            update(DataImportRun)
            .where(DataImportRun.id == run_id)
            .values(
                status=RUN_FAILED,
                error_code=code,
                error_message=message[:500],
                heartbeat_at=_utc_now_naive(),
                finished_at=_utc_now_naive(),
            )
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - 失败标记本身失败只能记录日志
        await db.rollback()
        logger.error("标记导入运行失败状态也失败 run_id=%s: %s", run_id, sanitize_error(exc))


async def _existing_conflicts(
    db: AsyncSession, rows: list[StagedRow]
) -> tuple[set[str], set[int]]:
    """只查询当前批次的订单号/顺序号，避免把全表或全文件 ID 装进内存。"""
    order_ids = [row.order_id for row in rows if row.status == ROW_VALID and row.order_id]
    seq_ids = [
        row.normalized["order_seq_id"]
        for row in rows
        if row.status == ROW_VALID
        and row.normalized is not None
        and row.normalized.get("order_seq_id")
    ]
    existing_orders: set[str] = set()
    existing_seqs: set[int] = set()
    if order_ids:
        result = await db.execute(select(Order.order_no).where(Order.order_no.in_(order_ids)))
        existing_orders = set(result.scalars().all())
    if seq_ids:
        result = await db.execute(select(Order.id).where(Order.id.in_(seq_ids)))
        existing_seqs = {int(value) for value in result.scalars().all()}
    return existing_orders, existing_seqs


def _row_values(row: StagedRow, run_id: int) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "row_no": row.row_no,
        "order_id": (row.order_id or "")[:50],
        "status": row.status,
        "error_code": (row.error_code or "")[:64],
        "error_message": (row.error_message or "")[:500],
        "normalized_json": row.normalized,
        "raw_json": row.raw,
    }


async def _stage_batch(
    db: AsyncSession,
    run_id: int,
    batch: list[StagedRow],
    seen: _SeenIds,
    *,
    rows_read: int,
) -> None:
    """冲突判定 + 批量写入暂存行 + 运行进度，同一个事务提交。"""
    # 文件内重复：后出现的行标记冲突，第一行仍有机会入库。
    for row in batch:
        if row.status != ROW_VALID:
            continue
        seq_id = row.normalized.get("order_seq_id") if row.normalized else None
        order_duplicate_of = seen.order_ids.get(row.order_id) if row.order_id else None
        seq_duplicate_of = seen.seq_ids.get(seq_id) if seq_id else None
        if order_duplicate_of:
            row.status = ROW_CONFLICT
            row.error_code = "DUPLICATE_ORDER_ID_IN_FILE"
            row.error_message = f"订单号与第 {order_duplicate_of} 行重复"
        elif seq_duplicate_of:
            row.status = ROW_CONFLICT
            row.error_code = "DUPLICATE_ORDER_SEQ_IN_FILE"
            row.error_message = f"订单顺序编号与第 {seq_duplicate_of} 行重复"
        # 冲突行同样登记，避免后续行用另一个唯一字段绕过文件内重复判定。
        if row.order_id:
            seen.order_ids.setdefault(row.order_id, row.row_no)
        if seq_id:
            seen.seq_ids.setdefault(seq_id, row.row_no)

    existing_orders, existing_seqs = await _existing_conflicts(db, batch)
    for row in batch:
        if row.status != ROW_VALID:
            continue
        seq_id = row.normalized.get("order_seq_id") if row.normalized else None
        if row.order_id and row.order_id in existing_orders:
            row.status = ROW_CONFLICT
            row.error_code = "ORDER_ID_EXISTS"
            row.error_message = "订单号已存在，v1 不支持覆盖更新"
        elif seq_id and seq_id in existing_seqs:
            row.status = ROW_CONFLICT
            row.error_code = "ORDER_SEQ_ID_EXISTS"
            row.error_message = "订单顺序编号已被占用"

    await db.execute(insert(DataImportRow.__table__), [_row_values(row, run_id) for row in batch])
    await db.execute(
        update(DataImportRun)
        .where(DataImportRun.id == run_id)
        .values(rows_read=rows_read, heartbeat_at=_utc_now_naive())
    )
    await db.commit()


async def _validate_rows(db: AsyncSession, run: DataImportRun, path: Path) -> None:
    """解析文件并暂存每一行；任何致命错误都向上冒泡，由调用方统一标记失败。"""
    source_type = run.source_type or "csv"
    headers = read_headers(path, source_type)
    mapping = build_column_mapping(headers)
    sources: dict[str, str] = mapping["source_columns"]

    max_rows = (
        settings.data_import_max_rows_xlsx
        if source_type == "xlsx"
        else settings.data_import_max_rows_csv
    )

    batch: list[StagedRow] = []
    seen = _SeenIds(order_ids={}, seq_ids={})
    rows_read = 0

    for row_no, cells in _iter_source_rows(path, source_type, headers):
        if _is_blank(cells):
            continue
        rows_read += 1
        if rows_read > max_rows:
            raise ImportFileError(
                "ROW_LIMIT_EXCEEDED",
                f"数据行超过上限 {max_rows} 行，请拆分文件后重试",
                http_status=413,
            )

        # 行宽不匹配通常意味着逗号/制表符串列，直接判为无效行而不是猜测对齐。
        if len(cells) > len(headers) and any(cell.strip() for cell in cells[len(headers):]):
            batch.append(
                StagedRow(
                    row_no=row_no,
                    order_id="",
                    status=ROW_INVALID,
                    normalized=None,
                    raw=_build_raw(headers, cells),
                    error_code="COLUMN_COUNT_MISMATCH",
                    error_message=f"第 {row_no} 行单元格数量多于表头",
                )
            )
            if len(batch) >= settings.data_import_chunk_size:
                await _stage_batch(db, run.id, batch, seen, rows_read=rows_read)
                batch.clear()
            continue

        overlong = next(
            (
                header
                for index, header in enumerate(headers)
                if header.strip()
                and index < len(cells)
                and len(cells[index]) > settings.data_import_max_cell_chars
            ),
            None,
        )
        raw = _build_raw(headers, cells)
        if overlong:
            batch.append(
                StagedRow(
                    row_no=row_no,
                    order_id="",
                    status=ROW_INVALID,
                    normalized=None,
                    raw=raw,
                    error_code="CELL_TOO_LONG",
                    error_message=f"单元格「{overlong}」超过 {settings.data_import_max_cell_chars} 个字符",
                )
            )
        else:
            normalized, errors = normalize_row(raw, sources)
            if errors:
                code, message = _first_error(errors)
                batch.append(
                    StagedRow(
                        row_no=row_no,
                        order_id=normalized.get("order_id") or "",
                        status=ROW_INVALID,
                        normalized=normalized,
                        raw=raw,
                        error_code=code,
                        error_message=message,
                    )
                )
            else:
                batch.append(
                    StagedRow(
                        row_no=row_no,
                        order_id=normalized["order_id"],
                        status=ROW_VALID,
                        normalized=normalized,
                        raw=None,
                    )
                )

        if len(batch) >= settings.data_import_chunk_size:
            await _stage_batch(db, run.id, batch, seen, rows_read=rows_read)
            batch.clear()

    if batch:
        await _stage_batch(db, run.id, batch, seen, rows_read=rows_read)
    if rows_read == 0:
        raise ImportFileError("IMPORT_EMPTY_FILE", "文件没有数据行")


async def _finalize_validation(db: AsyncSession, run_id: int) -> None:
    counts_result = await db.execute(
        select(DataImportRow.status, func.count())
        .where(DataImportRow.run_id == run_id)
        .group_by(DataImportRow.status)
    )
    counts = {status: int(total) for status, total in counts_result.all()}
    total = sum(counts.values())
    await db.execute(
        update(DataImportRun)
        .where(DataImportRun.id == run_id)
        .values(
            status=RUN_VALIDATED,
            rows_read=total,
            rows_valid=counts.get(ROW_VALID, 0),
            rows_conflict=counts.get(ROW_CONFLICT, 0),
            rows_invalid=counts.get(ROW_INVALID, 0),
            heartbeat_at=_utc_now_naive(),
            finished_at=_utc_now_naive(),
            error_code="",
            error_message="",
        )
    )
    await db.commit()


async def validate_import_run(run_id: int, temp_path: str) -> None:
    """后台校验任务：独立连接池与事务，结束后删除临时文件。"""
    path = Path(temp_path)
    try:
        async with import_session_factory() as db:
            run = await db.get(DataImportRun, run_id)
            if run is None or run.status != RUN_VALIDATING:
                return
            try:
                await _validate_rows(db, run, path)
                await _finalize_validation(db, run_id)
            except ImportFileError as exc:
                await _mark_run_failed(db, run_id, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - 校验任务不能把异常抛回已返回的请求
                logger.exception("导入校验失败 run_id=%s: %s", run_id, sanitize_error(exc))
                await _mark_run_failed(
                    db, run_id, "IMPORT_VALIDATION_ERROR", "文件校验失败，请检查文件后重试"
                )
    finally:
        path.unlink(missing_ok=True)


def build_run_status(run: DataImportRun) -> ImportRunStatus:
    """把 ORM 运行头转换为对外状态；不暴露文件路径等内部信息。"""
    rows_read = run.rows_read or 0
    done = (run.rows_inserted or 0) + (run.rows_skipped or 0) + (run.rows_rejected or 0)
    if run.status == RUN_COMPLETED:
        progress = 100.0
    elif run.status in (RUN_RUNNING, RUN_CANCELLED) and rows_read:
        # 取消后也展示真实已完成比例，避免被误读成 0 或 100。
        progress = min(round(done / rows_read * 100, 1), 99.9 if run.status == RUN_RUNNING else 100.0)
    else:
        progress = 0.0
    return ImportRunStatus(
        run_id=run.id,
        status=run.status,
        operator=run.operator,
        source_type=run.source_type,
        original_filename=run.original_filename,
        size_bytes=run.source_size_bytes or 0,
        sha256=run.source_sha256,
        conflict_policy=run.conflict_policy or CONFLICT_POLICY_REJECT,
        counts=ImportCounts(
            rows_read=rows_read,
            rows_valid=run.rows_valid or 0,
            rows_conflict=run.rows_conflict or 0,
            rows_invalid=run.rows_invalid or 0,
            rows_skipped=run.rows_skipped or 0,
            rows_rejected=run.rows_rejected or 0,
            rows_inserted=run.rows_inserted or 0,
            rows_refunded=run.rows_refunded or 0,
        ),
        started_at=run.started_at,
        finished_at=run.finished_at,
        heartbeat_at=run.heartbeat_at,
        error_code=run.error_code or "",
        error_message=run.error_message or "",
        can_confirm=run.status == RUN_VALIDATED and (run.rows_valid or 0) > 0,
        can_execute=run.status == RUN_CONFIRMED,
        cancel_requested=bool(run.cancel_requested),
        progress_percent=progress,
    )


def assert_run_owner(run: DataImportRun, principal_name: str, is_admin: bool) -> None:
    """非管理员只能查看/操作自己发起的导入，避免草稿内容跨主体暴露。"""
    if not is_admin and run.operator != principal_name:
        raise ImportFileError(
            "IMPORT_RUN_FORBIDDEN",
            "只能访问自己发起的导入任务",
            http_status=403,
        )


def _masked_fields(row: DataImportRow, can_view_pii: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    source = row.normalized_json or row.raw_json or {}
    if isinstance(source, dict):
        fields = dict(source)
    if not can_view_pii and fields.get("user_name"):
        # 复制后再脱敏，绝不原地修改可能来自缓存/数据库的字典。
        fields["user_name"] = mask_user_name(str(fields["user_name"]))
    return fields


def _row_item(row: DataImportRow, can_view_pii: bool) -> ImportRowItem:
    return ImportRowItem(
        row_no=row.row_no,
        status=row.status,
        order_id=row.order_id or "",
        error_code=row.error_code or "",
        error_message=row.error_message or "",
        fields=_masked_fields(row, can_view_pii),
    )


async def load_run(db: AsyncSession, run_id: int) -> DataImportRun:
    run = await db.get(DataImportRun, run_id)
    if run is None:
        raise ImportFileError("IMPORT_RUN_NOT_FOUND", "导入任务不存在", http_status=404)
    return run


async def cleanup_expired_imports(db: AsyncSession) -> tuple[int, int]:
    """清理超过保留期的导入记录与暂存行，返回 (删除行数, 删除任务数)。

    只在新建导入前触发一次，不引入调度器；活跃中的校验/执行任务绝不删除。
    每批最多 200 个任务，避免单次上传请求长时间占用事务。
    """
    ttl_days = settings.data_import_staging_ttl_days
    if ttl_days < 1:
        # 小于 1 天视为关闭清理，保留全部历史。
        return 0, 0
    cutoff = _utc_now_naive() - timedelta(days=ttl_days)
    expired_ids = list(
        (
            await db.execute(
                select(DataImportRun.id)
                .where(
                    DataImportRun.status.notin_(_ACTIVE_STATUSES),
                    func.coalesce(
                        DataImportRun.finished_at,
                        DataImportRun.heartbeat_at,
                        DataImportRun.started_at,
                    )
                    < cutoff,
                )
                .order_by(DataImportRun.id)
                .limit(_CLEANUP_BATCH_SIZE)
            )
        ).scalars()
    )
    if not expired_ids:
        return 0, 0
    rows_result = await db.execute(
        delete(DataImportRow).where(DataImportRow.run_id.in_(expired_ids))
    )
    runs_result = await db.execute(
        delete(DataImportRun).where(DataImportRun.id.in_(expired_ids))
    )
    await db.commit()
    return (rows_result.rowcount or 0, runs_result.rowcount or 0)


async def list_runs(
    db: AsyncSession,
    *,
    principal_name: str,
    is_admin: bool,
    page: int,
    page_size: int,
) -> ImportRunListResponse:
    """导入历史：普通账号仅本人任务，管理员可查看全部。"""
    if page < 1:
        raise ImportFileError("IMPORT_INVALID_PAGE", "页码必须从 1 开始")
    if page_size < 1 or page_size > 100:
        raise ImportFileError("IMPORT_INVALID_PAGE_SIZE", "每页最多 100 条")
    condition = () if is_admin else (DataImportRun.operator == principal_name,)
    total = int(
        await db.scalar(
            select(func.count()).select_from(DataImportRun).where(*condition)
        )
        or 0
    )
    total_pages = max((total + page_size - 1) // page_size, 0)
    result = await db.execute(
        select(DataImportRun)
        .where(*condition)
        .order_by(DataImportRun.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return ImportRunListResponse(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        items=[build_run_status(run) for run in result.scalars()],
    )


async def cancel_run(
    db: AsyncSession,
    run_id: int,
    principal_name: str,
    *,
    is_admin: bool,
) -> DataImportRun:
    """请求取消：执行中只置标记，由执行任务在分块提交后停止。

    已提交分块保留；已校验但未开始执行的任务可直接终止，不写入任何订单。
    """
    run = await load_run(db, run_id)
    assert_run_owner(run, principal_name, is_admin)
    if run.status == RUN_VALIDATING:
        raise ImportFileError(
            "IMPORT_CANCEL_NOT_ALLOWED",
            "文件仍在校验中，暂不支持取消，请等待校验结束后重试",
            http_status=409,
        )
    if run.status in (RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED):
        return run
    now = _utc_now_naive()
    if run.status == RUN_RUNNING:
        await db.execute(
            update(DataImportRun)
            .where(DataImportRun.id == run_id, DataImportRun.status == RUN_RUNNING)
            .values(cancel_requested=1, heartbeat_at=now)
        )
    else:
        await db.execute(
            update(DataImportRun)
            .where(DataImportRun.id == run_id)
            .values(
                status=RUN_CANCELLED,
                cancel_requested=1,
                finished_at=now,
                error_code="IMPORT_CANCELLED",
                error_message="已取消：尚未开始执行，未写入任何订单",
            )
        )
    await db.commit()
    await db.refresh(run)
    return run


async def build_preview(
    db: AsyncSession, run: DataImportRun, *, can_view_pii: bool
) -> ImportPreviewResponse:
    options = run.options_json if isinstance(run.options_json, dict) else {}
    columns = [
        ImportColumnMapping(
            canonical=item.get("canonical", ""),
            source=item.get("source"),
            required=bool(item.get("required")),
        )
        for item in options.get("columns", [])
        if isinstance(item, dict)
    ]
    limit = settings.data_import_preview_rows
    samples: dict[str, list[ImportRowItem]] = {}
    for row_status in _PREVIEW_STATUSES:
        result = await db.execute(
            select(DataImportRow)
            .where(DataImportRow.run_id == run.id, DataImportRow.status == row_status)
            .order_by(DataImportRow.row_no)
            .limit(limit)
        )
        samples[row_status] = [_row_item(row, can_view_pii) for row in result.scalars()]
    return ImportPreviewResponse(
        run=build_run_status(run),
        columns=columns,
        unknown_columns=list(options.get("unknown_columns", [])),
        missing_required=list(options.get("missing_required", [])),
        preview_limit=limit,
        valid=samples[ROW_VALID],
        conflict=samples[ROW_CONFLICT],
        invalid=samples[ROW_INVALID],
    )


async def build_row_page(
    db: AsyncSession,
    run: DataImportRun,
    *,
    row_status: str,
    page: int,
    page_size: int,
    can_view_pii: bool,
) -> ImportRowPage:
    if row_status not in _ROW_PAGE_STATUSES:
        raise ImportFileError("IMPORT_INVALID_ROW_STATUS", "不支持的行状态")
    if page < 1:
        raise ImportFileError("IMPORT_INVALID_PAGE", "页码必须从 1 开始")
    if page_size < 1 or page_size > settings.data_import_preview_rows:
        raise ImportFileError(
            "IMPORT_INVALID_PAGE_SIZE",
            f"每页最多 {settings.data_import_preview_rows} 行",
        )

    total = int(
        await db.scalar(
            select(func.count())
            .select_from(DataImportRow)
            .where(DataImportRow.run_id == run.id, DataImportRow.status == row_status)
        )
        or 0
    )
    total_pages = max((total + page_size - 1) // page_size, 0)
    result = await db.execute(
        select(DataImportRow)
        .where(DataImportRow.run_id == run.id, DataImportRow.status == row_status)
        .order_by(DataImportRow.row_no)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [_row_item(row, can_view_pii) for row in result.scalars()]
    return ImportRowPage(
        run_id=run.id,
        status=row_status,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        items=items,
    )


def create_temp_upload(suffix: str) -> Path:
    """创建 0600 临时文件；调用方负责在校验结束或异常路径删除。"""
    descriptor, name = tempfile.mkstemp(prefix="ea-import-", suffix=suffix)
    os.close(descriptor)
    return Path(name)


# ─────────────────── 二次确认与执行 ───────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def import_lock_is_free() -> bool:
    """探测 MySQL 命名锁；只用于同步返回 409，真正互斥由执行任务持有锁保证。"""
    async with import_session_factory() as session:
        acquired = await session.scalar(
            text("SELECT GET_LOCK(:name, 0)"), {"name": _IMPORT_LOCK_NAME}
        )
        if int(acquired or 0) != 1:
            return False
        await session.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": _IMPORT_LOCK_NAME})
        await session.commit()
        return True


async def confirm_run(
    db: AsyncSession,
    run_id: int,
    expected_sha256: str,
    principal_name: str,
    *,
    is_admin: bool,
) -> tuple[DataImportRun, str | None]:
    """原子把 validated 推进到 confirmed；重复确认轮换令牌，便于客户重试。"""
    run = await load_run(db, run_id)
    assert_run_owner(run, principal_name, is_admin)
    if expected_sha256.lower() != (run.source_sha256 or "").lower():
        raise ImportFileError(
            "IMPORT_SOURCE_MISMATCH",
            "文件摘要与当前导入任务不一致，请重新上传",
            http_status=409,
        )
    if run.status == RUN_VALIDATING:
        raise ImportFileError("IMPORT_NOT_VALIDATED", "文件仍在校验中，请稍后再确认", http_status=409)
    if run.status == RUN_FAILED:
        raise ImportFileError("IMPORT_RUN_FAILED", "导入任务已失败，不能确认", http_status=409)
    if run.status in (RUN_RUNNING, RUN_COMPLETED):
        return run, None
    if run.status not in (RUN_VALIDATED, RUN_CONFIRMED):
        raise ImportFileError("IMPORT_STATUS_CONFLICT", "当前状态不能确认", http_status=409)

    token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    now = _utc_now_naive()
    claimed = await db.execute(
        update(DataImportRun)
        .where(DataImportRun.id == run_id, DataImportRun.status == run.status)
        .values(
            status=RUN_CONFIRMED,
            confirmed_by=principal_name,
            confirmed_at=now,
            confirm_token_hash=token_hash,
            heartbeat_at=now,
        )
    )
    await db.commit()
    if (claimed.rowcount or 0) == 0:
        # 并发确认/执行抢先一步；重新读取后按最新状态返回，不覆盖令牌。
        await db.refresh(run)
        if run.status in (RUN_RUNNING, RUN_COMPLETED):
            return run, None
        raise ImportFileError("IMPORT_STATUS_CONFLICT", "导入状态已变化，请刷新后重试", http_status=409)
    await db.refresh(run)
    return run, token


async def request_execution(
    db: AsyncSession,
    run_id: int,
    confirm_token: str,
    principal_name: str,
    *,
    is_admin: bool,
) -> tuple[DataImportRun, bool]:
    """校验确认令牌并原子抢占 running；返回 (运行记录, 是否需要调度执行)。"""
    run = await load_run(db, run_id)
    assert_run_owner(run, principal_name, is_admin)
    if run.status in (RUN_RUNNING, RUN_COMPLETED):
        return run, False
    if run.status == RUN_FAILED:
        raise ImportFileError("IMPORT_RUN_FAILED", "导入任务已失败，不能执行", http_status=409)
    if run.status == RUN_VALIDATING:
        raise ImportFileError("IMPORT_NOT_VALIDATED", "文件仍在校验中", http_status=409)
    if run.status == RUN_VALIDATED:
        raise ImportFileError("IMPORT_NOT_CONFIRMED", "请先完成二次确认", http_status=409)
    if not run.confirm_token_hash or not hmac.compare_digest(
        run.confirm_token_hash, _token_hash(confirm_token)
    ):
        raise ImportFileError(
            "IMPORT_CONFIRM_TOKEN_INVALID",
            "确认令牌无效，请重新确认",
            http_status=403,
        )
    if not await import_lock_is_free():
        raise ImportFileError(
            "IMPORT_CONCURRENCY_CONFLICT",
            "已有导入正在执行，请稍后再试",
            http_status=409,
        )

    now = _utc_now_naive()
    claimed = await db.execute(
        update(DataImportRun)
        .where(
            DataImportRun.id == run_id,
            DataImportRun.status == RUN_CONFIRMED,
            DataImportRun.confirm_token_hash == run.confirm_token_hash,
        )
        .values(status=RUN_RUNNING, heartbeat_at=now)
    )
    await db.commit()
    if (claimed.rowcount or 0) == 0:
        await db.refresh(run)
        if run.status in (RUN_RUNNING, RUN_COMPLETED):
            return run, False
        raise ImportFileError("IMPORT_STATUS_CONFLICT", "导入状态已变化，请刷新后重试", http_status=409)
    await db.refresh(run)
    return run, True


async def _safe_invalidate_import_caches() -> None:
    """缓存失效失败不能把已提交的导入标记为失败，只记录告警。"""
    try:
        await invalidate_tags("analytics", "rfm")
        clear_rfm_snapshot_cache()
    except Exception as exc:  # noqa: BLE001 - 缓存是旁路
        logger.warning("导入后缓存失效失败: %s", sanitize_error(exc))


async def _data_version(db: AsyncSession) -> int | None:
    value = await db.scalar(text("SELECT data_version FROM data_state WHERE id = 1"))
    return int(value) if value is not None else None


def _event_id(source_sha256: str, row_no: int, event_type: str) -> str:
    """确定性事件 ID：重传同一文件/同一行时不会重复记账。"""
    return str(uuid.uuid5(_IMPORT_EVENT_NAMESPACE, f"{source_sha256}:{row_no}:{event_type}"))


def _wall_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _order_values(row: DataImportRow, now: datetime) -> dict[str, Any]:
    """把规范化暂存行转换为 orders 插入值；字段缺失即视为暂存行损坏。"""
    normalized = row.normalized_json
    if not isinstance(normalized, dict):
        raise ValueError("暂存行缺少规范化数据")
    refund = normalized.get("is_refund") == "是"
    return {
        "id": normalized.get("order_seq_id"),
        "order_id": normalized["order_id"],
        "user_name": normalized.get("user_name"),
        "product_id": normalized.get("product_id"),
        "order_amount": Decimal(normalized["order_amount"]),
        "payment_amount": Decimal(normalized["payment_amount"]),
        "channel_id": normalized.get("channel_id"),
        "platform_type": normalized["platform_type"],
        "order_time": _wall_datetime(normalized.get("order_time")),
        "payment_time": _wall_datetime(normalized.get("payment_time")),
        "is_refunded": "是" if refund else "否",
        "discount_amount": Decimal(normalized.get("discount_amount") or "0.00"),
        "payment_duration_sec": normalized.get("payment_duration_sec") or 0,
        "order_date": date.fromisoformat(normalized["order_date"]),
        "order_hour": normalized.get("order_hour"),
        "weekday": normalized.get("weekday"),
        "order_status": "REFUNDED" if refund else "ACTIVE",
        "event_version": 2 if refund else 1,
        "updated_at": now,
    }


def _ledger_events(row: DataImportRow, run: DataImportRun, now: datetime) -> list[dict[str, Any]]:
    """每个导入订单补记 ORDER_CREATED；退款行再补记 v2 的 ORDER_REFUNDED。"""
    normalized = row.normalized_json
    if not isinstance(normalized, dict):
        return []
    order_time = _wall_datetime(normalized.get("order_time"))
    payment_time = _wall_datetime(normalized.get("payment_time")) or order_time
    if order_time is None:
        return []
    payload = {
        "user_name": normalized.get("user_name"),
        "product_id": normalized.get("product_id"),
        "order_amount": normalized.get("order_amount"),
        "payment_amount": normalized.get("payment_amount"),
        "platform_type": normalized.get("platform_type"),
        "channel_id": normalized.get("channel_id"),
        "discount_amount": normalized.get("discount_amount"),
        "order_time": normalized.get("order_time"),
    }
    events = [
        {
            "event_id": _event_id(run.source_sha256, row.row_no, "ORDER_CREATED"),
            "order_id": row.order_id,
            "event_type": "ORDER_CREATED",
            "occurred_at": order_time,
            "sequence": 1,
            "source": "data_import",
            "payload": payload,
            "processing_status": "PROCESSED",
            "processed_at": now,
        }
    ]
    if normalized.get("is_refund") == "是":
        events.append(
            {
                "event_id": _event_id(run.source_sha256, row.row_no, "ORDER_REFUNDED"),
                "order_id": row.order_id,
                "event_type": "ORDER_REFUNDED",
                "occurred_at": payment_time or order_time,
                "sequence": 2,
                "source": "data_import",
                "payload": {"reason": "数据导入：源文件标记为退款"},
                "processing_status": "PROCESSED",
                "processed_at": now,
            }
        )
    return events


async def _existing_conflicts_for_rows(
    db: AsyncSession, rows: list[DataImportRow]
) -> tuple[set[str], set[int]]:
    """执行前再次查询冲突，覆盖“校验后、确认前”其他写路径插入的数据。"""
    order_ids = [row.order_id for row in rows if row.order_id]
    seq_ids = [
        row.normalized_json.get("order_seq_id")
        for row in rows
        if isinstance(row.normalized_json, dict) and row.normalized_json.get("order_seq_id")
    ]
    existing_orders: set[str] = set()
    existing_seqs: set[int] = set()
    if order_ids:
        result = await db.execute(select(Order.order_no).where(Order.order_no.in_(order_ids)))
        existing_orders = set(result.scalars().all())
    if seq_ids:
        result = await db.execute(select(Order.id).where(Order.id.in_(seq_ids)))
        existing_seqs = {int(value) for value in result.scalars().all()}
    return existing_orders, existing_seqs


async def _insert_orders_safely(
    db: AsyncSession, pairs: list[tuple[DataImportRow, dict[str, Any]]]
) -> tuple[list[DataImportRow], list[tuple[DataImportRow, str, str]]]:
    """批量插入，唯一键冲突时回退到逐行插入，只让冲突行失败。"""
    values = [value for _, value in pairs]
    try:
        async with db.begin_nested():
            await db.execute(insert(Order.__table__), values)
        return [row for row, _ in pairs], []
    except IntegrityError:
        inserted: list[DataImportRow] = []
        failed: list[tuple[DataImportRow, str, str]] = []
        for row, value in pairs:
            try:
                async with db.begin_nested():
                    await db.execute(insert(Order.__table__), [value])
                inserted.append(row)
            except IntegrityError:
                failed.append((row, "ORDER_CONFLICT", "订单号或顺序编号与已有数据冲突"))
        return inserted, failed


async def _mark_rows_failed(
    db: AsyncSession, run_id: int, failed: list[tuple[DataImportRow, str, str]]
) -> None:
    for row, code, message in failed:
        await db.execute(
            update(DataImportRow)
            .where(DataImportRow.run_id == run_id, DataImportRow.row_no == row.row_no)
            .values(status=ROW_FAILED, error_code=code, error_message=message[:500])
        )


async def _apply_conflict_policy(db: AsyncSession, run: DataImportRun) -> None:
    """冲突行在 v1 只有两种去向：失败并继续，或按策略跳过；绝不覆盖已有订单。"""
    count = int(
        await db.scalar(
            select(func.count())
            .select_from(DataImportRow)
            .where(DataImportRow.run_id == run.id, DataImportRow.status == ROW_CONFLICT)
        )
        or 0
    )
    if count == 0:
        return
    if run.conflict_policy == CONFLICT_POLICY_SKIP:
        await db.execute(
            update(DataImportRow)
            .where(DataImportRow.run_id == run.id, DataImportRow.status == ROW_CONFLICT)
            .values(
                status=ROW_SKIPPED,
                error_code="CONFLICT_SKIPPED",
                error_message="冲突行按策略跳过，未写入订单",
            )
        )
        await db.execute(
            update(DataImportRun)
            .where(DataImportRun.id == run.id)
            .values(rows_skipped=DataImportRun.rows_skipped + count)
        )
    else:
        await db.execute(
            update(DataImportRow)
            .where(DataImportRow.run_id == run.id, DataImportRow.status == ROW_CONFLICT)
            .values(status=ROW_FAILED)
        )
        await db.execute(
            update(DataImportRun)
            .where(DataImportRun.id == run.id)
            .values(rows_rejected=DataImportRun.rows_rejected + count)
        )
    await db.commit()


async def _execute_chunk(db: AsyncSession, run: DataImportRun, rows: list[DataImportRow]) -> None:
    """一个事务内完成 orders 插入、事件补记、版本递增、进度与行状态更新。"""
    now = _utc_now_naive()
    existing_orders, existing_seqs = await _existing_conflicts_for_rows(db, rows)
    candidates: list[tuple[DataImportRow, dict[str, Any]]] = []
    failed: list[tuple[DataImportRow, str, str]] = []
    for row in rows:
        normalized = row.normalized_json
        seq_id = normalized.get("order_seq_id") if isinstance(normalized, dict) else None
        try:
            values = _order_values(row, now)
        except (KeyError, TypeError, ValueError):
            failed.append((row, "INVALID_STAGED_ROW", "暂存行数据不完整，请重新上传"))
            continue
        if row.order_id in existing_orders:
            failed.append((row, "ORDER_ID_EXISTS", "订单号已存在，v1 不支持覆盖更新"))
        elif seq_id and seq_id in existing_seqs:
            failed.append((row, "ORDER_SEQ_ID_EXISTS", "订单顺序编号已被占用"))
        else:
            candidates.append((row, values))

    inserted, insert_failed = await _insert_orders_safely(db, candidates)
    failed.extend(insert_failed)

    if inserted:
        events: list[dict[str, Any]] = []
        for row in inserted:
            events.extend(_ledger_events(row, run, now))
        if events:
            # 确定性 event_id 已存在时保持原账本行不变，保证重放幂等。
            event_stmt = mysql_insert(OrderEvent.__table__).values(events)
            event_stmt = event_stmt.on_duplicate_key_update(
                event_id=OrderEvent.__table__.c.event_id
            )
            await db.execute(event_stmt)
        await db.execute(
            update(DataImportRow)
            .where(
                DataImportRow.run_id == run.id,
                DataImportRow.row_no.in_([row.row_no for row in inserted]),
            )
            .values(status=ROW_IMPORTED, error_code="", error_message="")
        )
        await db.execute(text("UPDATE data_state SET data_version = data_version + 1 WHERE id = 1"))

    await _mark_rows_failed(db, run.id, failed)
    refunded = sum(1 for row in inserted if row.normalized_json.get("is_refund") == "是")
    await db.execute(
        update(DataImportRun)
        .where(DataImportRun.id == run.id)
        .values(
            rows_inserted=DataImportRun.rows_inserted + len(inserted),
            rows_refunded=DataImportRun.rows_refunded + refunded,
            rows_rejected=DataImportRun.rows_rejected + len(failed),
            heartbeat_at=now,
        )
    )
    await db.commit()
    await _safe_invalidate_import_caches()


async def _consistency_mismatch_count(db: AsyncSession, run_id: int) -> int:
    """只读校验：导入行的 orders 水位/状态/退款标记必须与事件账本一致。"""
    value = await db.scalar(
        text(
            """
            SELECT COUNT(*)
            FROM data_import_rows r
            JOIN orders o ON o.order_id = r.order_id
            WHERE r.run_id = :run_id
              AND r.status = 'imported'
              AND (
                o.event_version <> (
                    SELECT COALESCE(MAX(e.sequence), 0)
                    FROM order_events e
                    WHERE e.order_id = o.order_id
                )
                OR (o.is_refund = '是' AND (o.order_status <> 'REFUNDED' OR o.event_version <> 2))
                OR (o.is_refund <> '是' AND (o.order_status <> 'ACTIVE' OR o.event_version <> 1))
                OR (
                    o.is_refund = '是' AND NOT EXISTS (
                        SELECT 1 FROM order_events e
                        WHERE e.order_id = o.order_id AND e.event_type = 'ORDER_REFUNDED'
                    )
                )
                OR (
                    o.is_refund <> '是' AND EXISTS (
                        SELECT 1 FROM order_events e
                        WHERE e.order_id = o.order_id AND e.event_type = 'ORDER_REFUNDED'
                    )
                )
              )
            """
        ),
        {"run_id": run_id},
    )
    return int(value or 0)


async def _is_cancel_requested(db: AsyncSession, run_id: int) -> bool:
    """读取取消标记；每个分块提交后检查一次，避免跨事务缓存旧值。"""
    return bool(
        await db.scalar(
            select(DataImportRun.cancel_requested).where(DataImportRun.id == run_id)
        )
    )


async def _finalize_execution(
    db: AsyncSession, run: DataImportRun, *, cancelled: bool = False
) -> None:
    counts_result = await db.execute(
        select(DataImportRow.status, func.count())
        .where(DataImportRow.run_id == run.id)
        .group_by(DataImportRow.status)
    )
    counts = {status: int(total) for status, total in counts_result.all()}
    imported = counts.get(ROW_IMPORTED, 0)
    refunded = int(
        await db.scalar(
            text(
                "SELECT COUNT(*) FROM data_import_rows "
                "WHERE run_id = :run_id AND status = 'imported' "
                "AND JSON_UNQUOTE(JSON_EXTRACT(normalized_json, '$.is_refund')) = '是'"
            ),
            {"run_id": run.id},
        )
        or 0
    )
    mismatches = await _consistency_mismatch_count(db, run.id)
    now = _utc_now_naive()
    values = {
        "rows_inserted": imported,
        "rows_refunded": refunded,
        "rows_skipped": counts.get(ROW_SKIPPED, 0),
        "rows_rejected": counts.get(ROW_FAILED, 0),
        "data_version_after": await _data_version(db),
        "heartbeat_at": now,
        "finished_at": now,
    }
    if mismatches:
        values.update(
            status=RUN_FAILED,
            error_code="IMPORT_CONSISTENCY_FAILED",
            error_message=f"导入后一致性校验失败：{mismatches} 行的订单状态与事件账本不一致",
        )
    elif cancelled:
        values.update(
            status=RUN_CANCELLED,
            error_code="IMPORT_CANCELLED",
            error_message="已取消：已提交的分块保留，未处理的行未写入",
        )
    else:
        values.update(status=RUN_COMPLETED, error_code="", error_message="")
    await db.execute(update(DataImportRun).where(DataImportRun.id == run.id).values(**values))
    await db.commit()


async def _mark_execution_failed(db: AsyncSession, run_id: int, code: str, message: str) -> None:
    """执行失败时保留已有暂存行与已完成的分块，供失败导出与排查使用。"""
    now = _utc_now_naive()
    await db.execute(
        update(DataImportRun)
        .where(DataImportRun.id == run_id)
        .values(
            status=RUN_FAILED,
            error_code=code,
            error_message=message[:500],
            heartbeat_at=now,
            finished_at=now,
        )
    )
    await db.commit()


async def _execute_run(db: AsyncSession, run: DataImportRun) -> None:
    chunk_size = max(100, min(settings.data_import_chunk_size, 1000))
    before = await _data_version(db)
    await db.execute(
        update(DataImportRun)
        .where(DataImportRun.id == run.id)
        .values(data_version_before=before, heartbeat_at=_utc_now_naive())
    )
    await db.commit()

    await _apply_conflict_policy(db, run)

    cancelled = await _is_cancel_requested(db, run.id)
    while not cancelled:
        result = await db.execute(
            select(DataImportRow)
            .where(DataImportRow.run_id == run.id, DataImportRow.status == ROW_VALID)
            .order_by(DataImportRow.row_no)
            .limit(chunk_size)
        )
        rows = list(result.scalars())
        if not rows:
            break
        await _execute_chunk(db, run, rows)
        # 只在分块提交后读取标记：已提交的分块不会被回滚。
        cancelled = await _is_cancel_requested(db, run.id)

    await _finalize_execution(db, run, cancelled=cancelled)
    await _safe_invalidate_import_caches()


async def execute_import_run(run_id: int) -> None:
    """后台执行任务：持有 MySQL GET_LOCK，分块提交，逐块失效缓存。"""
    async with import_session_factory() as db:
        run = await db.get(DataImportRun, run_id)
        if run is None or run.status != RUN_RUNNING:
            return
        acquired = await db.scalar(
            text("SELECT GET_LOCK(:name, 0)"), {"name": _IMPORT_LOCK_NAME}
        )
        if int(acquired or 0) != 1:
            await _mark_execution_failed(
                db, run_id, "IMPORT_CONCURRENCY_CONFLICT", "已有导入正在执行，本次未启动"
            )
            return
        try:
            await _execute_run(db, run)
        except ImportFileError as exc:
            await _mark_execution_failed(db, run_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛回已返回的请求
            await db.rollback()
            logger.exception("导入执行失败 run_id=%s: %s", run_id, sanitize_error(exc))
            await _mark_execution_failed(
                db, run_id, "IMPORT_EXECUTION_ERROR", "导入执行失败，请查看失败明细后重试"
            )
        finally:
            await db.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": _IMPORT_LOCK_NAME})
            await db.commit()


async def build_execution_result(
    db: AsyncSession, run: DataImportRun, *, can_view_pii: bool
) -> ImportResultResponse:
    failed_result = await db.execute(
        select(DataImportRow)
        .where(DataImportRow.run_id == run.id, DataImportRow.status == ROW_FAILED)
        .order_by(DataImportRow.row_no)
        .limit(settings.data_import_preview_rows)
    )
    skipped = int(
        await db.scalar(
            select(func.count())
            .select_from(DataImportRow)
            .where(DataImportRow.run_id == run.id, DataImportRow.status == ROW_SKIPPED)
        )
        or 0
    )
    return ImportResultResponse(
        run=build_run_status(run),
        failed=[_row_item(row, can_view_pii) for row in failed_result.scalars()],
        skipped=skipped,
    )


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> str:
    """防止 Excel 把订单号/错误信息当成公式执行。"""
    text_value = "" if value is None else str(value)
    if text_value.lstrip().startswith(_FORMULA_PREFIXES):
        return "'" + text_value
    return text_value


async def export_failure_csv(db: AsyncSession, run: DataImportRun) -> tuple[bytes, bool]:
    """导出 invalid/failed 明细；返回 (CSV 内容, 是否因上限被截断)。"""
    limit = settings.data_import_error_export_max_rows
    result = await db.execute(
        select(DataImportRow)
        .where(
            DataImportRow.run_id == run.id,
            DataImportRow.status.in_(_FAILED_EXPORT_STATUSES),
        )
        .order_by(DataImportRow.row_no)
        .limit(limit + 1)
    )
    rows = list(result.scalars())
    truncated = len(rows) > limit
    rows = rows[:limit]

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        [
            "行号",
            "订单号",
            "行状态",
            "错误码",
            "错误信息",
            "订单金额",
            "付款金额",
            "平台类型",
            "下单时间",
        ]
    )
    for row in rows:
        fields = row.normalized_json if isinstance(row.normalized_json, dict) else {}
        writer.writerow(
            [
                _csv_safe(row.row_no),
                _csv_safe(row.order_id),
                _csv_safe(row.status),
                _csv_safe(row.error_code),
                _csv_safe(row.error_message),
                _csv_safe(fields.get("order_amount")),
                _csv_safe(fields.get("payment_amount")),
                _csv_safe(fields.get("platform_type")),
                _csv_safe(fields.get("order_time")),
            ]
        )
    # UTF-8 BOM 让 Excel 直接识别中文；BOM 必须在所有内容之前。
    return ("\ufeff" + buffer.getvalue()).encode("utf-8"), truncated
