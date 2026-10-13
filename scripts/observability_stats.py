#!/usr/bin/env python
"""Langfuse trace 统计：日查询量 / 平均 token 成本 / P50·P95 延迟。

数据来源为 Langfuse Public API（自托管实例），不直接读数据库。
未配置 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST 时
明确报错退出——不编造数字（延续仓库诚实传统）。

用法：
    python scripts/observability_stats.py                 # 默认统计最近 7 天
    python scripts/observability_stats.py --days 30
    python scripts/observability_stats.py --limit 500     # 最多拉取的 trace 数

单价估算（可选）：环境变量 LANGFUSE_PRICE_INPUT_PER_MTOK /
LANGFUSE_PRICE_OUTPUT_PER_MTOK（美元/百万 token）。Langfuse UI 配置了
model price map 时 costDetails.totalCost 已含成本，优先采用。
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys

import httpx

API_TIMEOUT = httpx.Timeout(30.0)
PAGE_SIZE = 100


def _require_env() -> tuple[str, str, str]:
    host = os.getenv("LANGFUSE_HOST", "").rstrip("/")
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    missing = [
        name
        for name, value in (
            ("LANGFUSE_HOST", host),
            ("LANGFUSE_PUBLIC_KEY", public_key),
            ("LANGFUSE_SECRET_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        print(
            "❌ 未配置 Langfuse 连接信息，无法统计。请设置环境变量：\n"
            f"   缺失：{', '.join(missing)}\n"
            "   参考 docs/observability.md 的部署与配置说明。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return host, public_key, secret_key


def _client(public_key: str, secret_key: str) -> httpx.Client:
    return httpx.Client(
        auth=(public_key, secret_key),
        timeout=API_TIMEOUT,
        headers={"Accept": "application/json"},
    )


def fetch_traces(client: httpx.Client, host: str, *, since: dt.datetime, limit: int) -> list[dict]:
    """分页拉取 trace 列表，直到覆盖时间范围或达到 limit。"""
    traces: list[dict] = []
    page = 1
    while len(traces) < limit:
        resp = client.get(
            f"{host}/api/public/traces",
            params={"limit": PAGE_SIZE, "page": page},
        )
        resp.raise_for_status()
        payload = resp.json()
        batch = payload.get("data", [])
        if not batch:
            break
        reached_cutoff = False
        for item in batch:
            timestamp = _parse_timestamp(item.get("timestamp"))
            if timestamp is not None and timestamp < since:
                reached_cutoff = True
                continue
            traces.append(item)
        total_pages = payload.get("meta", {}).get("totalPages", page)
        if page >= total_pages or reached_cutoff:
            break
        page += 1
    return traces[:limit]


def _parse_timestamp(raw) -> dt.datetime | None:
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _usage_numbers(details: dict | None) -> tuple[int, float | None]:
    """从 usageDetails / costDetails 提取 token 数与成本。

    Langfuse 不同版本的字段名有差异（inputTokens vs input），
    这里对常见键名做容错解析；解析不到 token 时返回 0（不编造）。
    """
    if not isinstance(details, dict):
        return 0, None
    tokens = 0
    for key in ("total", "totalTokens", "total_tokens"):
        if isinstance(details.get(key), (int, float)):
            tokens = int(details[key])
            break
    else:
        tokens = int(
            sum(
                int(details[key])
                for key in ("input", "output", "inputTokens", "outputTokens")
                if isinstance(details.get(key), (int, float))
            )
        )
    return tokens, None


def fetch_trace_detail(client: httpx.Client, host: str, trace_id: str) -> dict | None:
    try:
        resp = client.get(f"{host}/api/public/traces/{trace_id}")
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        print(f"⚠️ trace {trace_id} 详情拉取失败，已跳过：{type(exc).__name__}", file=sys.stderr)
        return None


def collect_stats(client: httpx.Client, host: str, traces: list[dict]) -> dict[str, dict]:
    """按天聚合：查询量、token、成本、延迟样本。"""
    price_in = os.getenv("LANGFUSE_PRICE_INPUT_PER_MTOK")
    price_out = os.getenv("LANGFUSE_PRICE_OUTPUT_PER_MTOK")
    by_day: dict[str, dict] = {}
    for trace in traces:
        timestamp = _parse_timestamp(trace.get("timestamp"))
        if timestamp is None:
            continue
        day = timestamp.astimezone().strftime("%Y-%m-%d")
        detail = fetch_trace_detail(client, host, trace["id"])
        if detail is None:
            continue
        observations = detail.get("observations", [])
        total_tokens = 0
        total_cost = 0.0
        has_cost = False
        for obs in observations:
            if obs.get("type") not in ("GENERATION", "generation"):
                continue
            usage = obs.get("usageDetails") or obs.get("usage") or {}
            tokens, _ = _usage_numbers(usage)
            total_tokens += tokens
            cost_detail = obs.get("costDetails") or obs.get("cost") or {}
            if isinstance(cost_detail, dict) and isinstance(cost_detail.get("totalCost"), (int, float)):
                total_cost += float(cost_detail["totalCost"])
                has_cost = True
        # trace 级延迟：优先 trace.latency（秒），否则用首末 observation 推算
        latency = detail.get("latency")
        if not isinstance(latency, (int, float)):
            starts = [_parse_timestamp(o.get("startTime")) for o in observations]
            ends = [_parse_timestamp(o.get("endTime")) for o in observations]
            starts = [t for t in starts if t]
            ends = [t for t in ends if t]
            latency = (max(ends) - min(starts)).total_seconds() if starts and ends else 0.0
        if not has_cost and price_in and price_out and total_tokens:
            # 仅配置了单价且 UI 无成本时按总量线性估算（口径见报告）
            total_cost = total_tokens / 1_000_000 * (float(price_in) + float(price_out)) / 2
            has_cost = True

        bucket = by_day.setdefault(day, {"count": 0, "tokens": 0, "cost": 0.0, "has_cost": False, "latencies": []})
        bucket["count"] += 1
        bucket["tokens"] += total_tokens
        bucket["latencies"].append(float(latency))
        if has_cost:
            bucket["cost"] += total_cost
            bucket["has_cost"] = True
    return by_day


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return ordered[min(rank, len(ordered) - 1)]


def print_report(by_day: dict[str, dict]) -> None:
    if not by_day:
        print("（时间范围内没有查询 trace）")
        return
    header = f"{'日期':<12}{'查询量':>8}{'总 token':>12}{'平均 token':>12}{'P50 延迟(s)':>14}{'P95 延迟(s)':>14}{'成本(USD)':>12}"
    print(header)
    print("-" * len(header.expandtabs()))
    total_count = total_tokens = 0
    total_cost = 0.0
    all_latencies: list[float] = []
    for day in sorted(by_day):
        bucket = by_day[day]
        avg_tokens = bucket["tokens"] / bucket["count"]
        p50 = _percentile(bucket["latencies"], 50)
        p95 = _percentile(bucket["latencies"], 95)
        cost_text = f"{bucket['cost']:.4f}" if bucket["has_cost"] else "-"
        print(
            f"{day:<12}{bucket['count']:>8}{bucket['tokens']:>12}"
            f"{avg_tokens:>12.0f}{p50:>14.2f}{p95:>14.2f}{cost_text:>12}"
        )
        total_count += bucket["count"]
        total_tokens += bucket["tokens"]
        total_cost += bucket["cost"]
        all_latencies.extend(bucket["latencies"])
    print("-" * len(header.expandtabs()))
    print(
        f"{'合计':<12}{total_count:>8}{total_tokens:>12}"
        f"{total_tokens / total_count:>12.0f}"
        f"{_percentile(all_latencies, 50):>14.2f}{_percentile(all_latencies, 95):>14.2f}"
        f"{total_cost:>12.4f}"
    )
    print("\n口径说明：")
    print("- 查询量：统计范围内 trace 数（一次用户提问 = 一条 trace）")
    print("- token：trace 内所有 GENERATION observation 的 usage 合计")
    print("- 延迟：trace 从开始到结束的墙钟时间（Langfuse trace.latency）")
    print("- 成本：Langfuse UI 配置 model price map 时取 costDetails；")
    print("  否则用 LANGFUSE_PRICE_* 单价按 token 线性估算，估算行以近似值为准")


def main() -> None:
    parser = argparse.ArgumentParser(description="Langfuse trace 日统计（查询量/token/延迟/成本）")
    parser.add_argument("--days", type=int, default=7, help="统计最近 N 天（默认 7）")
    parser.add_argument("--limit", type=int, default=500, help="最多拉取的 trace 数（默认 500）")
    args = parser.parse_args()

    host, public_key, secret_key = _require_env()
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)

    with _client(public_key, secret_key) as client:
        try:
            traces = fetch_traces(client, host, since=since, limit=args.limit)
        except httpx.HTTPError as exc:
            print(f"❌ 拉取 trace 列表失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            print("请检查 LANGFUSE_HOST 是否可达、Key 是否有效。", file=sys.stderr)
            raise SystemExit(1)
        print(f"统计范围：最近 {args.days} 天，共拉取 {len(traces)} 条 trace\n")
        by_day = collect_stats(client, host, traces)
    print_report(by_day)


if __name__ == "__main__":
    main()
