"""可复现的合成订单事件流生成器。

示例（仅本机）：
    python scripts/order_event_producer.py --token <JWT> --rps 10 --duration 30

脚本从公开 bootstrap CSV 仅抽取商品/渠道枚举，不发送其中的用户名、订单号或任何
个人信息；每个订单号和用户名均由固定种子生成。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

import httpx

_NAMESPACE = UUID("e37ae5df-ea8f-4a6c-a5e7-0db343f860a5")
_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_URL = "http://127.0.0.1:8000/api/order-events"


def _percentile(samples: list[float], percentile: float) -> float | None:
    if not samples:
        return None
    values = sorted(samples)
    index = min(len(values) - 1, round((len(values) - 1) * percentile))
    return round(values[index], 3)


def _catalog() -> list[dict[str, str]]:
    """只取公开样本的非个人维度，缺失时提供稳定 fallback。"""
    path = _ROOT / "data" / "cleaned_orders.csv"
    if not path.exists():
        return [{"product_id": "PRODUCT-001", "platform_type": "APP", "channel_id": "synthetic"}]
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        values = [
            {
                "product_id": row.get("商品编号") or row.get("product_id") or "PRODUCT-001",
                "platform_type": row.get("平台类型") or row.get("platform_type") or "APP",
                "channel_id": row.get("渠道编号") or row.get("channel_id") or "synthetic",
            }
            for _, row in zip(range(2000), rows)
        ]
    return values or [{"product_id": "PRODUCT-001", "platform_type": "APP", "channel_id": "synthetic"}]


def build_events(
    *, seed: int, count: int, refund_rate: float, duplicate_rate: float,
    out_of_order_rate: float, invalid_rate: float,
) -> list[dict]:
    """生成确定性事件序列；相同参数下 event_id、payload 与发送顺序完全一致。"""
    rng = random.Random(seed)
    catalog = _catalog()
    events: list[dict] = []
    # 事件发生时间也固定，确保请求体可重放；不依赖运行机器当前时钟。
    now = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seed)
    for index in range(count):
        order_id = f"SYN-{seed:08x}-{index:08d}"
        item = rng.choice(catalog)
        amount = round(rng.uniform(20, 500), 2)
        created = {
            "event_id": str(uuid5(_NAMESPACE, f"{seed}:create:{index}")),
            "order_id": order_id,
            "event_type": "ORDER_CREATED",
            "occurred_at": now.isoformat(),
            "version": 1,
            "source": "synthetic-producer",
            "payload": {
                "user_name": f"synthetic_user_{index % 1000:04d}",
                "product_id": item["product_id"],
                "order_amount": f"{amount:.2f}",
                "payment_amount": f"{amount:.2f}",
                "discount_amount": "0.00",
                "platform_type": item["platform_type"],
                "channel_id": item["channel_id"],
            },
        }
        follow_up: dict | None = None
        if rng.random() < refund_rate:
            follow_up = {
                "event_id": str(uuid5(_NAMESPACE, f"{seed}:refund:{index}")),
                "order_id": order_id,
                "event_type": "ORDER_REFUNDED",
                "occurred_at": now.isoformat(),
                "version": 2,
                "source": "synthetic-producer",
                "payload": {"reason": "synthetic_refund"},
            }
        if rng.random() < invalid_rate:
            # 故意违反金额约束，用于验证 422 与调用方 reject 统计。
            created["payload"]["payment_amount"] = "-1.00"
        pair = [created] + ([follow_up] if follow_up else [])
        if follow_up and rng.random() < out_of_order_rate:
            pair.reverse()
        events.extend(pair)
        if rng.random() < duplicate_rate:
            events.append(dict(created))
    return events


async def send_events(args: argparse.Namespace) -> dict:
    event_count = max(1, int(args.rps * args.duration))
    events = build_events(
        seed=args.seed,
        count=event_count,
        refund_rate=args.refund_rate,
        duplicate_rate=args.duplicate_rate,
        out_of_order_rate=args.out_of_order_rate,
        invalid_rate=args.invalid_rate,
    )
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    queue: asyncio.Queue[tuple[int, dict]] = asyncio.Queue()
    for index, event in enumerate(events):
        queue.put_nowait((index, event))

    results: Counter[str] = Counter()
    latencies: list[float] = []
    started = time.perf_counter()
    interval = 1 / max(args.rps, 0.1)

    async def worker(client: httpx.AsyncClient) -> None:
        while True:
            try:
                index, event = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            target = started + index * interval
            await asyncio.sleep(max(0, target - time.perf_counter()))
            t0 = time.perf_counter()
            try:
                response = await client.post(args.url, json=event, headers=headers)
                latencies.append((time.perf_counter() - t0) * 1000)
                if response.status_code == 200:
                    status = response.json()["results"][0]["status"]
                    results[status] += 1
                elif response.status_code == 422:
                    results["rejected"] += 1
                else:
                    results[f"http_{response.status_code}"] += 1
            except httpx.HTTPError:
                results["network_error"] += 1
            finally:
                queue.task_done()

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        await asyncio.gather(*(worker(client) for _ in range(args.concurrency)))
    elapsed = time.perf_counter() - started
    return {
        "seed": args.seed,
        "url": args.url,
        "attempted": len(events),
        "processed": results["processed"],
        "duplicates": results["duplicate"],
        "rejected": results["rejected"],
        "errors": sum(value for key, value in results.items() if key.startswith("http_") or key == "network_error"),
        "elapsed_seconds": round(elapsed, 3),
        "throughput_rps": round(len(events) / elapsed, 3) if elapsed else 0,
        "latency_ms": {"p50": _percentile(latencies, 0.50), "p95": _percentile(latencies, 0.95), "p99": _percentile(latencies, 0.99)},
        "result_counts": dict(results),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="可复现的本地合成订单事件流")
    parser.add_argument("--url", default=_DEFAULT_URL, help="默认仅指向 localhost")
    parser.add_argument("--token", default="", help="本地测试 JWT；不写入输出")
    parser.add_argument("--seed", type=int, default=20260918)
    parser.add_argument("--rps", type=float, default=10.0)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--duplicate-rate", type=float, default=0.05)
    parser.add_argument("--out-of-order-rate", type=float, default=0.02)
    parser.add_argument("--invalid-rate", type=float, default=0.01)
    parser.add_argument("--refund-rate", type=float, default=0.10)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output", type=Path, help="可选 JSON 结果文件")
    args = parser.parse_args()
    if not args.url.startswith(("http://127.0.0.1", "http://localhost")):
        parser.error("默认安全策略只允许 localhost；如需其他环境请显式修改脚本并完成审查")
    if args.rps <= 0 or args.duration <= 0 or args.concurrency <= 0:
        parser.error("rps、duration、concurrency 必须大于 0")
    for name in ("duplicate_rate", "out_of_order_rate", "invalid_rate", "refund_rate"):
        if not 0 <= getattr(args, name) <= 1:
            parser.error(f"{name} 必须在 0 到 1 之间")
    return args


if __name__ == "__main__":
    options = parse_args()
    report = asyncio.run(send_events(options))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if options.output:
        options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
