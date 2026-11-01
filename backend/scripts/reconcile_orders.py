"""历史退款状态对账：把「is_refund=是 且 ACTIVE 且无事件」的订单标记为 REFUNDED。

默认 dry-run；apply 需要显式确认。仅处理没有事件账本、没有运行期水位的历史行，
逐条写入 order_status_reconciliation 审计，不修改金额、is_refund 或账本。

注意：当前指标查询把 ACTIVE 作为有效销售额、把退款金额统计在 ACTIVE 内。
在指标口径迁移完成前直接 apply 会让退款金额归零，因此必须与指标版本一起上线。
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger("reconcile_orders")

APPLY_CONFIRMATION = "RECONCILE ORDER STATUS"
_LOCK_NAME = "ea_orders_reconcile"
_BATCH_SIZE = 1000

_CANDIDATE_SQL = """
SELECT o.order_seq_id, o.order_id
FROM orders AS o
WHERE o.is_refund = '是'
  AND o.order_status = 'ACTIVE'
  AND o.event_version = 0
  AND o.updated_at IS NULL
  AND o.order_seq_id > :after_seq
  AND NOT EXISTS (
      SELECT 1 FROM order_events AS e WHERE e.order_id = o.order_id
  )
ORDER BY o.order_seq_id
LIMIT :limit
"""


def _count_candidates(conn) -> int:
    return int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM orders AS o "
                "WHERE o.is_refund = '是' AND o.order_status = 'ACTIVE' "
                "AND o.event_version = 0 AND o.updated_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM order_events AS e "
                "WHERE e.order_id = o.order_id)"
            )
        ).scalar()
        or 0
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


def run_reconcile(
    *,
    apply: bool,
    confirm: str | None,
    operator: str,
    database_url: str | None = None,
    max_rows: int | None = None,
) -> dict:
    if apply and confirm != APPLY_CONFIRMATION:
        raise ValueError(
            f"apply 需要显式确认：--confirm \"{APPLY_CONFIRMATION}\""
        )

    if database_url is None:
        from backend.scripts.import_orders import database_url_from_env

        database_url = database_url_from_env()

    engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=1800)
    lock_conn = None
    try:
        lock_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        acquired = lock_conn.execute(
            text("SELECT GET_LOCK(:name, 5)"), {"name": _LOCK_NAME}
        ).scalar()
        if acquired != 1:
            raise RuntimeError("另一个状态对账任务正在执行，请稍后重试")
        for table in ("orders", "order_events", "order_status_reconciliation", "data_state"):
            if not _table_exists(lock_conn, table):
                raise RuntimeError(f"{table} 不存在，请先执行 alembic upgrade head")

        total = _count_candidates(lock_conn)
        limit = total if max_rows is None else min(total, max(1, max_rows))
        result = {
            "apply": apply,
            "candidates": total,
            "selected": limit,
            "updated": 0,
            "skipped": 0,
            "run_id": None,
        }
        if not apply or limit == 0:
            return result

        with engine.begin() as conn:
            run_id = int(
                conn.execute(
                    text(
                        "INSERT INTO data_import_runs "
                        "(source, source_sha256, mode, status, operator, rows_read, started_at) "
                        "VALUES (:source, :sha, 'reconcile', 'running', :operator, :rows, :ts)"
                    ),
                    {
                        "source": "reconcile:is_refund=是",
                        "sha": "0" * 64,
                        "operator": operator[:100],
                        "rows": limit,
                        "ts": datetime.now(),
                    },
                ).lastrowid
            )
            result["run_id"] = run_id

            updated = 0
            skipped = 0
            processed = 0
            after_seq = 0
            while processed < limit:
                batch = conn.execute(
                    text(_CANDIDATE_SQL),
                    {
                        "limit": min(_BATCH_SIZE, limit - processed),
                        "after_seq": after_seq,
                    },
                ).all()
                if not batch:
                    break
                for seq_id, order_id in batch:
                    after_seq = max(after_seq, int(seq_id))
                    processed += 1
                    changed = conn.execute(
                        text(
                            "UPDATE orders SET order_status = 'REFUNDED', "
                            "updated_at = :now, event_version = event_version "
                            "WHERE order_id = :order_id AND order_status = 'ACTIVE' "
                            "AND event_version = 0 AND updated_at IS NULL"
                        ),
                        {"order_id": order_id, "now": datetime.now()},
                    ).rowcount
                    if not changed:
                        skipped += 1
                        continue
                    conn.execute(
                        text(
                            "INSERT INTO order_status_reconciliation "
                            "(run_id, order_id, old_status, new_status, reason, created_at) "
                            "VALUES (:run_id, :order_id, 'ACTIVE', 'REFUNDED', :reason, :ts)"
                        ),
                        {
                            "run_id": run_id,
                            "order_id": order_id,
                            "reason": "历史 is_refund=是 且无事件账本",
                            "ts": datetime.now(),
                        },
                    )
                    updated += 1

            conn.execute(
                text("UPDATE data_state SET data_version = data_version + 1 WHERE id = 1")
            )
            conn.execute(
                text(
                    "UPDATE data_import_runs SET status = 'completed', "
                    "rows_imported = :updated, finished_at = :ts, notes = :notes "
                    "WHERE id = :id"
                ),
                {
                    "updated": updated,
                    "ts": datetime.now(),
                    "notes": f"skipped={skipped}",
                    "id": run_id,
                },
            )

        result["updated"] = updated
        result["skipped"] = skipped
        return result
    finally:
        if lock_conn is not None:
            try:
                lock_conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": _LOCK_NAME})
            except Exception:  # noqa: BLE001
                logger.warning("释放对账锁失败", exc_info=True)
            lock_conn.close()
        engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="历史退款状态对账（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="实际写入状态与审计")
    parser.add_argument(
        "--confirm",
        default=None,
        help=f"apply 必须传入 {APPLY_CONFIRMATION}",
    )
    parser.add_argument(
        "--operator",
        default=os.getenv("IMPORT_OPERATOR") or getpass.getuser(),
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--max-rows", type=int, default=None, help="调试用：最多处理前 N 行")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    args = _build_parser().parse_args(argv)
    try:
        result = run_reconcile(
            apply=args.apply,
            confirm=args.confirm,
            operator=args.operator,
            database_url=args.database_url,
            max_rows=args.max_rows,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 统一输出
        print(json.dumps({"status": "failed", "error": str(exc)[:1000]}, ensure_ascii=False))
        logger.error("状态对账失败: %s", exc)
        return 1
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
