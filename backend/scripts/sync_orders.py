"""已停用的启动同步入口。

历史实现会在容器启动时用 CSV 全量替换 orders，运行期创建的订单、退款、
取消以及事件账本会被静默覆盖。该行为已移除；现在导入只能由运维显式执行：

    python backend/scripts/import_orders.py --mode dry-run
    python backend/scripts/import_orders.py --mode bootstrap
    python backend/scripts/import_orders.py --mode replace --confirm "REPLACE ORDER DATA"
"""
from __future__ import annotations

import logging

logger = logging.getLogger("sync_orders")


def sync_orders() -> bool:
    raise RuntimeError(
        "启动时订单同步已停用，避免覆盖运行期订单与事件账本；"
        "请使用 backend/scripts/import_orders.py 显式导入"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    try:
        sync_orders()
    except RuntimeError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc
