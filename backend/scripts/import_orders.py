"""安全导入订单 CSV：默认 bootstrap，全量替换需显式确认且禁止覆盖运行期数据。

设计约束（与启动同步解耦）：
- 后端启动不再导入或替换 orders；本脚本只能由运维显式执行；
- bootstrap 仅允许在 orders 与 order_events 均为空时执行；
- replace 仅允许在事件账本为空且不存在运行期订单
  （event_version > 0 或 updated_at 非空）时执行；
- replace 先建 staging 表并校验，再把原表重命名为带时间戳的备份表，
  备份不自动删除；失败时原 orders 表保持不变；
- 历史 CSV 行保持 ACTIVE/version=0；是否退款仍由 is_refund 表达，
  状态对账由 reconcile_orders.py 在指标口径升级后单独执行。
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

from sqlalchemy import create_engine, text

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger("import_orders")

CSV_ENCODING = "utf-8-sig"
REQUIRED_HEADERS = {
    "订单顺序编号",
    "订单号",
    "用户名",
    "商品编号",
    "订单金额",
    "付款金额",
    "渠道编号",
    "平台类型",
    "下单时间",
    "付款时间",
    "是否退款",
    "优惠金额",
    "支付耗时_秒",
    "下单日期",
    "下单小时",
    "星期几",
}
REFUND_VALUES = {"是", "否"}
REPLACE_CONFIRMATION = "REPLACE ORDER DATA"
IMPORT_LOCK_NAME = "ea_orders_import"
MAX_REPORTED_ERRORS = 10

_INSERT_COLUMNS = (
    "order_seq_id",
    "order_id",
    "user_name",
    "product_id",
    "order_amount",
    "payment_amount",
    "channel_id",
    "platform_type",
    "order_time",
    "payment_time",
    "is_refund",
    "discount_amount",
    "payment_duration_sec",
    "order_date",
    "order_hour",
    "weekday",
    "order_status",
    "event_version",
    "updated_at",
)
_INSERT_SQL = (
    "INSERT INTO {table} ("
    + ", ".join(f"`{name}`" for name in _INSERT_COLUMNS)
    + ") VALUES ("
    + ", ".join(f":{name}" for name in _INSERT_COLUMNS)
    + ")"
)


class ImportErrorBase(RuntimeError):
    """导入流程的可预期失败。"""


class CsvValidationError(ImportErrorBase):
    pass


class ImportGuardError(ImportErrorBase):
    pass


@dataclass(frozen=True)
class CsvStats:
    rows: int
    refund_rows: int
    payment_sum: Decimal
    min_order_date: date | None
    max_order_date: date | None
    sha256: str

    def as_dict(self) -> dict:
        return {
            "rows": self.rows,
            "refund_rows": self.refund_rows,
            "payment_sum": str(self.payment_sum),
            "min_order_date": self.min_order_date.isoformat() if self.min_order_date else None,
            "max_order_date": self.max_order_date.isoformat() if self.max_order_date else None,
            "sha256": self.sha256,
        }


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _parse_int(value: str, *, field: str, row_no: int, allow_none: bool = True) -> int | None:
    raw = _clean(value)
    if not raw:
        if allow_none:
            return None
        raise CsvValidationError(f"第 {row_no} 行 {field} 不能为空")
    try:
        return int(float(raw))
    except ValueError as exc:
        raise CsvValidationError(f"第 {row_no} 行 {field} 不是整数") from exc


def _parse_decimal(value: str, *, field: str, row_no: int) -> Decimal | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise CsvValidationError(f"第 {row_no} 行 {field} 不是合法金额") from exc
    if number < 0:
        raise CsvValidationError(f"第 {row_no} 行 {field} 为负数")
    return number


def _parse_datetime(value: str, *, field: str, row_no: int) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CsvValidationError(f"第 {row_no} 行 {field} 不是合法时间") from exc


def _parse_date(value: str, *, field: str, row_no: int) -> date | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise CsvValidationError(f"第 {row_no} 行 {field} 不是合法日期") from exc


def parse_row(row: dict[str, str], row_no: int) -> dict:
    order_id = _clean(row.get("订单号"))
    if not order_id:
        raise CsvValidationError(f"第 {row_no} 行 订单号 不能为空")
    if len(order_id) > 50:
        raise CsvValidationError(f"第 {row_no} 行 订单号 超过 50 字符")

    refund = _clean(row.get("是否退款")) or None
    if refund is not None and refund not in REFUND_VALUES:
        raise CsvValidationError(f"第 {row_no} 行 是否退款 只能是 是/否")

    order_hour = _parse_int(row.get("下单小时"), field="下单小时", row_no=row_no)
    if order_hour is not None and not 0 <= order_hour <= 23:
        raise CsvValidationError(f"第 {row_no} 行 下单小时 超出 0-23")

    return {
        "order_seq_id": _parse_int(
            row.get("订单顺序编号"), field="订单顺序编号", row_no=row_no, allow_none=False
        ),
        "order_id": order_id,
        "user_name": _clean(row.get("用户名")) or None,
        "product_id": _clean(row.get("商品编号")) or None,
        "order_amount": _parse_decimal(row.get("订单金额"), field="订单金额", row_no=row_no),
        "payment_amount": _parse_decimal(
            row.get("付款金额"), field="付款金额", row_no=row_no
        ),
        "channel_id": _clean(row.get("渠道编号")) or None,
        "platform_type": _clean(row.get("平台类型")) or None,
        "order_time": _parse_datetime(row.get("下单时间"), field="下单时间", row_no=row_no),
        "payment_time": _parse_datetime(row.get("付款时间"), field="付款时间", row_no=row_no),
        "is_refund": refund,
        "discount_amount": _parse_decimal(
            row.get("优惠金额"), field="优惠金额", row_no=row_no
        ),
        "payment_duration_sec": _parse_int(
            row.get("支付耗时_秒"), field="支付耗时_秒", row_no=row_no
        ),
        "order_date": _parse_date(row.get("下单日期"), field="下单日期", row_no=row_no),
        "order_hour": order_hour,
        "weekday": _clean(row.get("星期几")) or None,
        # 历史 CSV 不携带事件水位；状态推进只能由事件账本完成。
        "order_status": "ACTIVE",
        "event_version": 0,
        "updated_at": None,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_csv_rows(path: Path):
    """逐行解析 CSV；调用方负责汇总统计，避免把大文件整体留在内存。"""
    with path.open("r", encoding=CSV_ENCODING, newline="") as file:
        reader = csv.DictReader(file)
        headers = set(reader.fieldnames or [])
        missing = REQUIRED_HEADERS - headers
        if missing:
            raise CsvValidationError(f"订单 CSV 缺少列: {', '.join(sorted(missing))}")
        for row_no, row in enumerate(reader, start=2):
            yield row_no, parse_row(row, row_no)


def scan_csv(path: Path) -> CsvStats:
    """完整校验 CSV 并返回统计信息；发现错误时不建立任何数据库对象。"""
    if not path.is_file():
        raise CsvValidationError(f"订单 CSV 不存在: {path}")

    errors: list[str] = []
    rows = 0
    refund_rows = 0
    payment_sum = Decimal("0")
    min_order_date: date | None = None
    max_order_date: date | None = None
    seen_order_ids: set[str] = set()
    seen_seq_ids: set[int] = set()

    try:
        for row_no, parsed in _iter_csv_rows(path):
            order_id = parsed["order_id"]
            seq_id = parsed["order_seq_id"]
            if order_id in seen_order_ids:
                errors.append(f"第 {row_no} 行 订单号重复: {order_id}")
            if seq_id in seen_seq_ids:
                errors.append(f"第 {row_no} 行 订单顺序编号重复")
            seen_order_ids.add(order_id)
            seen_seq_ids.add(seq_id)

            rows += 1
            if parsed["is_refund"] == "是":
                refund_rows += 1
            payment_sum += (parsed["payment_amount"] or Decimal("0")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            order_date = parsed["order_date"]
            if order_date is not None:
                min_order_date = (
                    order_date if min_order_date is None else min(min_order_date, order_date)
                )
                max_order_date = (
                    order_date if max_order_date is None else max(max_order_date, order_date)
                )

            if len(errors) >= MAX_REPORTED_ERRORS:
                break
    except CsvValidationError:
        raise
    except csv.Error as exc:
        raise CsvValidationError(f"订单 CSV 解析失败: {exc}") from exc

    if errors:
        raise CsvValidationError("；".join(errors[:MAX_REPORTED_ERRORS]))
    if rows == 0:
        raise CsvValidationError("订单 CSV 没有数据行")
    if len(seen_order_ids) != rows:
        raise CsvValidationError("订单号去重后行数不一致")

    return CsvStats(
        rows=rows,
        refund_rows=refund_rows,
        payment_sum=payment_sum,
        min_order_date=min_order_date,
        max_order_date=max_order_date,
        sha256=_file_sha256(path),
    )


def _insert_csv(engine, table: str, path: Path, *, batch_size: int, expected_rows: int) -> None:
    insert_sql = text(_INSERT_SQL.format(table=table))
    batch: list[dict] = []
    inserted = 0
    with engine.begin() as conn:
        for _, parsed in _iter_csv_rows(path):
            batch.append(parsed)
            if len(batch) >= batch_size:
                conn.execute(insert_sql, batch)
                inserted += len(batch)
                batch.clear()
        if batch:
            conn.execute(insert_sql, batch)
            inserted += len(batch)
    if inserted != expected_rows:
        raise CsvValidationError(
            f"CSV 在导入过程中发生变化: expected={expected_rows}, inserted={inserted}"
        )


def database_url_from_env() -> str:
    """运维脚本直接读取连接变量，避免触发 Web 服务的生产凭据校验。"""
    user = os.getenv("MIGRATION_DB_USER") or os.getenv("DB_USER") or "root"
    password = os.getenv("MIGRATION_DB_PASSWORD")
    if password is None:
        password = os.getenv("DB_PASSWORD", "")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "3306")
    name = os.getenv("DB_NAME", "ai_commerce_intelligence_platform")
    return (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}?charset=utf8mb4"
    )


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _safe_error(exc: BaseException) -> str:
    message = re.sub(r"://[^@\s]+@", "://***@", str(exc))
    return message[:2000]


def _log_run_started(
    engine, *, source: str, sha256: str, mode: str, operator: str, rows_read: int
) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO data_import_runs "
                "(source, source_sha256, mode, status, operator, rows_read, started_at) "
                "VALUES (:source, :sha256, :mode, 'running', :operator, :rows_read, :started_at)"
            ),
            {
                "source": source[:512],
                "sha256": sha256,
                "mode": mode,
                "operator": operator[:100],
                "rows_read": rows_read,
                "started_at": datetime.now(),
            },
        )
        return int(result.lastrowid)


def _log_run_finished(
    engine,
    run_id: int,
    *,
    status: str,
    rows_imported: int,
    backup_table: str | None = None,
    error_message: str | None = None,
    notes: str | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE data_import_runs SET status = :status, rows_imported = :rows_imported, "
                "backup_table = :backup_table, finished_at = :finished_at, "
                "error_message = :error_message, notes = :notes WHERE id = :id"
            ),
            {
                "status": status,
                "rows_imported": rows_imported,
                "backup_table": backup_table,
                "finished_at": datetime.now(),
                "error_message": error_message,
                "notes": notes,
                "id": run_id,
            },
        )


def _guard(conn, mode: str) -> None:
    if not _table_exists(conn, "orders") or not _table_exists(conn, "order_events"):
        raise ImportGuardError("orders/order_events 不存在，请先执行 alembic upgrade head")
    if not _table_exists(conn, "data_import_runs") or not _table_exists(conn, "data_state"):
        raise ImportGuardError("数据审计表不存在，请先执行 alembic upgrade head")

    order_count = int(conn.execute(text("SELECT COUNT(*) FROM orders")).scalar() or 0)
    event_count = int(conn.execute(text("SELECT COUNT(*) FROM order_events")).scalar() or 0)

    if mode == "bootstrap":
        if order_count or event_count:
            raise ImportGuardError(
                f"bootstrap 仅允许空库（orders={order_count}, order_events={event_count}）；"
                "已有数据请使用 replace（需确认）或先停止写入后评估"
            )
        return

    if mode == "replace":
        runtime_orders = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM orders "
                    "WHERE event_version <> 0 OR updated_at IS NOT NULL"
                )
            ).scalar()
            or 0
        )
        if event_count or runtime_orders:
            raise ImportGuardError(
                "检测到运行期订单或事件账本，禁止全量替换："
                f"order_events={event_count}, runtime_orders={runtime_orders}；"
                "历史数据更新请使用独立合并/对账流程"
            )
        return

    raise ImportGuardError(f"未知导入模式: {mode}")


def _validate_staging(conn, staging: str, stats: CsvStats) -> None:
    row = conn.execute(
        text(
            f"SELECT COUNT(*) AS rows_count, COUNT(DISTINCT order_id) AS distinct_orders, "
            f"SUM(payment_amount) AS payment_sum, MIN(order_date) AS min_date, "
            f"MAX(order_date) AS max_date, "
            f"SUM(CASE WHEN is_refund = '是' THEN 1 ELSE 0 END) AS refund_rows "
            f"FROM `{staging}`"
        )
    ).one()
    if int(row.rows_count) != stats.rows:
        raise CsvValidationError(
            f"staging 行数不一致: expected={stats.rows}, actual={row.rows_count}"
        )
    if int(row.distinct_orders) != stats.rows:
        raise CsvValidationError(
            f"staging 订单号不唯一: rows={row.rows_count}, distinct={row.distinct_orders}"
        )
    if Decimal(row.payment_sum or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != stats.payment_sum:
        raise CsvValidationError(
            f"staging 付款金额合计不一致: expected={stats.payment_sum}, "
            f"actual={row.payment_sum}"
        )
    if int(row.refund_rows or 0) != stats.refund_rows:
        raise CsvValidationError(
            f"staging 退款行数不一致: expected={stats.refund_rows}, "
            f"actual={row.refund_rows}"
        )
    if stats.min_order_date and row.min_date != stats.min_order_date:
        raise CsvValidationError("staging 最小下单日期与 CSV 不一致")
    if stats.max_order_date and row.max_date != stats.max_order_date:
        raise CsvValidationError("staging 最大下单日期与 CSV 不一致")


def run_import(
    *,
    mode: str,
    csv_path: Path,
    operator: str,
    database_url: str | None = None,
    batch_size: int = 1000,
    confirm: str | None = None,
) -> dict:
    if mode not in {"bootstrap", "replace", "dry-run"}:
        raise ImportGuardError(f"未知导入模式: {mode}")
    if mode == "replace" and confirm != REPLACE_CONFIRMATION:
        raise ImportGuardError(
            f"replace 需要显式确认：--confirm \"{REPLACE_CONFIRMATION}\""
        )

    stats = scan_csv(csv_path)
    if mode == "dry-run":
        return {
            "mode": mode,
            "status": "dry-run",
            "source": str(csv_path.resolve()),
            "stats": stats.as_dict(),
        }

    if database_url is None:
        database_url = database_url_from_env()

    engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=1800)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid4().hex[:6]
    staging = f"orders_staging_{timestamp}_{suffix}"
    backup = f"orders_backup_{timestamp}_{suffix}"
    run_id: int | None = None
    lock_conn = None
    renamed = False

    try:
        lock_conn = engine.connect()
        lock_conn = lock_conn.execution_options(isolation_level="AUTOCOMMIT")
        acquired = lock_conn.execute(
            text("SELECT GET_LOCK(:name, 5)"), {"name": IMPORT_LOCK_NAME}
        ).scalar()
        if acquired != 1:
            raise ImportGuardError("另一个订单导入任务正在执行，请稍后重试")

        _guard(lock_conn, mode)
        run_id = _log_run_started(
            engine,
            source=str(csv_path.resolve()),
            sha256=stats.sha256,
            mode=mode,
            operator=operator,
            rows_read=stats.rows,
        )

        lock_conn.execute(text(f"DROP TABLE IF EXISTS `{staging}`"))
        lock_conn.execute(text(f"CREATE TABLE `{staging}` LIKE `orders`"))
        _insert_csv(
            engine,
            staging,
            csv_path,
            batch_size=batch_size,
            expected_rows=stats.rows,
        )

        _validate_staging(lock_conn, staging, stats)
        # 换表前再次检查，缩小与并发事件写入的竞争窗口。
        if mode == "replace":
            _guard(lock_conn, mode)

        if mode == "bootstrap":
            # bootstrap 已确认原表为空；仍保留一份空表备份以维持原子换表语义。
            lock_conn.execute(
                text(f"RENAME TABLE `orders` TO `{backup}`, `{staging}` TO `orders`")
            )
        else:
            lock_conn.execute(
                text(f"RENAME TABLE `orders` TO `{backup}`, `{staging}` TO `orders`")
            )
        renamed = True

        with engine.begin() as conn:
            conn.execute(text("UPDATE data_state SET data_version = data_version + 1 WHERE id = 1"))
            conn.execute(
                text(
                    "INSERT INTO app_metadata (meta_key, meta_value) "
                    "VALUES ('orders_csv_sha256', :sha), ('orders_imported_at', :ts) "
                    "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)"
                ),
                {"sha": stats.sha256, "ts": datetime.now().isoformat(timespec="seconds")},
            )
        _log_run_finished(
            engine,
            run_id,
            status="completed",
            rows_imported=stats.rows,
            backup_table=backup,
            notes=f"mode={mode}; staging={staging}",
        )
        return {
            "mode": mode,
            "status": "completed",
            "run_id": run_id,
            "rows_imported": stats.rows,
            "backup_table": backup,
            "source": str(csv_path.resolve()),
            "stats": stats.as_dict(),
        }
    except Exception as exc:
        if lock_conn is not None and not renamed:
            try:
                lock_conn.execute(text(f"DROP TABLE IF EXISTS `{staging}`"))
            except Exception:  # noqa: BLE001 - 清理失败不掩盖原始错误
                logger.exception("清理 staging 表失败")
        if run_id is not None:
            try:
                _log_run_finished(
                    engine,
                    run_id,
                    status="failed",
                    rows_imported=0,
                    error_message=_safe_error(exc),
                )
            except Exception:  # noqa: BLE001
                logger.exception("写入失败审计失败")
        raise
    finally:
        if lock_conn is not None:
            try:
                lock_conn.execute(
                    text("SELECT RELEASE_LOCK(:name)"), {"name": IMPORT_LOCK_NAME}
                )
            except Exception:  # noqa: BLE001
                logger.warning("释放导入锁失败", exc_info=True)
            lock_conn.close()
        engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安全导入订单 CSV（bootstrap / replace / dry-run）"
    )
    parser.add_argument(
        "--mode",
        choices=("bootstrap", "replace", "dry-run"),
        default="dry-run",
        help="bootstrap 仅空库；replace 需确认且禁止覆盖运行期数据",
    )
    parser.add_argument(
        "--csv",
        default=os.getenv("ORDERS_CSV_PATH", str(_ROOT / "data" / "cleaned_orders.csv")),
        help="订单 CSV 路径",
    )
    parser.add_argument(
        "--operator",
        default=os.getenv("IMPORT_OPERATOR") or getpass.getuser(),
        help="审计记录中的操作人",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="单批插入行数",
    )
    parser.add_argument(
        "--confirm",
        default=None,
        help=f"replace 模式必须传入 {REPLACE_CONFIRMATION}",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="覆盖导入连接串（默认使用 MIGRATION_DB_* / 主库管理账号）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    args = _build_parser().parse_args(argv)
    try:
        result = run_import(
            mode=args.mode,
            csv_path=Path(args.csv),
            operator=args.operator,
            database_url=args.database_url,
            batch_size=max(1, args.batch_size),
            confirm=args.confirm,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 统一输出
        print(json.dumps({"status": "failed", "error": _safe_error(exc)}, ensure_ascii=False))
        logger.error("订单导入失败: %s", _safe_error(exc))
        return 1
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
