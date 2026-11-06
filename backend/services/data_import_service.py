"""业务数据导入：文件识别、流式解析、行级校验、暂存与预览。

设计约束：
- CSV 使用 csv.reader 逐行读取，XLSX 使用 openpyxl read_only，禁止 pandas 全量载入；
- 校验阶段把所有明细写入 data_import_rows，后续确认/执行只读暂存行，不再重新解析文件；
- 运行状态、计数、错误码都落库，进程重启后仍可查询；
- 原始文件只落在临时目录，校验结束立即删除，不覆盖 data/cleaned_orders.csv。
"""

from __future__ import annotations

import csv
import logging
import os
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import load_workbook
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database import import_session_factory
from backend.models.database_models import DataImportRow, DataImportRun, Order
from backend.models.import_schemas import (
    ImportColumnMapping,
    ImportCounts,
    ImportPreviewResponse,
    ImportRowItem,
    ImportRowPage,
    ImportRunStatus,
)
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
    CanonicalField("user_name", ("用户名", "user_name"), False),
    CanonicalField("product_id", ("商品编号", "product_id"), False),
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
    product_id = _clean_text(value_of("product_id"), "商品编号", 50, errors)
    channel_id = _clean_text(value_of("channel_id"), "渠道编号", 50, errors)
    platform_type = _clean_text(value_of("platform_type"), "平台类型", 50, errors)
    if not platform_type:
        errors.append(("REQUIRED_FIELD", "平台类型不能为空"))

    order_amount = _parse_money(value_of("order_amount"), "订单金额", errors, required=True)
    payment_amount = _parse_money(value_of("payment_amount"), "付款金额", errors, required=True)
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
    elif run.status == RUN_RUNNING and rows_read:
        progress = min(round(done / rows_read * 100, 1), 99.9)
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
