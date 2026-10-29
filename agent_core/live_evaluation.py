"""显式运行真实模型的 Text-to-SQL 与知识回答评测。"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from sqlglot import parse_one, transpile

from agent_core.model_adapter import OpenAIModelAdapter
from agent_core.rag import MarkdownKnowledgeRetriever
from agent_core.sql_safety import validate_and_limit_sql

DEFAULT_DATASET = Path(__file__).with_name("eval") / "live_model_cases.jsonl"
DEFAULT_CSV = Path(__file__).resolve().parents[1] / "data" / "cleaned_orders.csv"
DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "ai-ecommerce-assistant" / "knowledge_base"

_COLUMN_MAP = {
    "订单顺序编号": "order_seq_id",
    "订单号": "order_id",
    "用户名": "user_name",
    "商品编号": "product_id",
    "订单金额": "order_amount",
    "付款金额": "payment_amount",
    "渠道编号": "channel_id",
    "平台类型": "platform_type",
    "下单时间": "order_time",
    "付款时间": "payment_time",
    "是否退款": "is_refund",
    "优惠金额": "discount_amount",
    "支付耗时_秒": "payment_duration_sec",
    "下单日期": "order_date",
    "下单小时": "order_hour",
    "星期几": "weekday",
}

_SCHEMA = """CREATE TABLE orders (
  order_seq_id INTEGER,
  order_id TEXT,
  user_name TEXT,
  product_id TEXT,
  order_amount REAL,
  payment_amount REAL,
  channel_id TEXT,
  platform_type TEXT,
  order_time DATETIME,
  payment_time DATETIME,
  is_refund TEXT,
  discount_amount REAL,
  payment_duration_sec INTEGER,
  order_date DATE,
  order_hour INTEGER,
  weekday TEXT
)"""

_BUSINESS_CONTEXT = """数据范围为 2025-01-01 至 2025-12-31，仅用于解释“最近”问题。
用户没有明确指定时间时，SQL 中禁止出现 order_time/order_date 条件，禁止自动添加 2025 全年过滤。
销售额使用 payment_amount；订单量必须 COUNT(DISTINCT order_id)，禁止 COUNT(*)；客单价 = SUM(payment_amount) / COUNT(DISTINCT order_id)。
除非用户明确要求排除退款，否则销售额、订单量、用户数等指标必须包含退款订单，不得自动添加 is_refund <> '是'。
用户明确指定月份或日期时必须严格使用该范围，不得扩大为全年。
仅生成 MySQL 兼容的单条只读 SELECT/WITH 查询。"""


@dataclass(frozen=True, slots=True)
class LiveCase:
    id: str
    kind: Literal["sql", "answer"]
    query: str
    reference_sql: str | None = None
    expected_keywords: tuple[str, ...] = ()


def load_live_cases(path: Path = DEFAULT_DATASET) -> list[LiveCase]:
    cases: list[LiveCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            payload["expected_keywords"] = tuple(payload.get("expected_keywords", ()))
            case = LiveCase(**payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"真实模型评测集第 {line_number} 行格式错误: {exc}") from exc
        if case.kind == "sql" and not case.reference_sql:
            raise ValueError(f"SQL 评测项 {case.id} 缺少 reference_sql")
        if case.kind == "answer" and not case.expected_keywords:
            raise ValueError(f"回答评测项 {case.id} 缺少 expected_keywords")
        cases.append(case)
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("真实模型评测集不能为空且 id 必须唯一")
    return cases


def build_sqlite_database(csv_path: Path) -> sqlite3.Connection:
    frame = pd.read_csv(csv_path).rename(columns=_COLUMN_MAP)
    missing = set(_COLUMN_MAP.values()) - set(frame.columns)
    if missing:
        raise ValueError(f"订单 CSV 缺少字段：{sorted(missing)}")
    connection = sqlite3.connect(":memory:")
    frame[list(_COLUMN_MAP.values())].to_sql("orders", connection, index=False)
    return connection


def _normalize_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.2f}"
    return str(value)


def canonical_rows(rows: list[tuple[Any, ...]], *, ordered: bool = False) -> list[tuple[str, ...]]:
    """保留列位置；仅在结果无排序要求时忽略行顺序。"""
    normalized = [tuple(_normalize_value(value) for value in row) for row in rows]
    return normalized if ordered else sorted(normalized)


def _to_sqlite(mysql_sql: str) -> str:
    safe_sql = validate_and_limit_sql(mysql_sql)
    return transpile(safe_sql, read="mysql", write="sqlite")[0]


def _execute(connection: sqlite3.Connection, mysql_sql: str) -> list[tuple[Any, ...]]:
    return connection.execute(_to_sqlite(mysql_sql)).fetchall()


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


async def evaluate_live_model(
    *,
    adapter: OpenAIModelAdapter,
    cases: list[LiveCase],
    csv_path: Path = DEFAULT_CSV,
    knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR,
    request_delay_seconds: float = 0.5,
) -> dict[str, Any]:
    connection = build_sqlite_database(csv_path)
    data_rows = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    retriever = MarkdownKnowledgeRetriever(knowledge_dir)
    details: list[dict[str, Any]] = []
    latencies: list[int] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    try:
        for case in cases:
            started = time.perf_counter()
            detail: dict[str, Any] = {"id": case.id, "kind": case.kind, "query": case.query}
            try:
                responses = []
                if case.kind == "sql":
                    previous_error = None
                    candidate_rows: list[tuple[Any, ...]] = []
                    for attempt in range(2):
                        response = await adapter.generate_sql(
                            case.query,
                            _SCHEMA,
                            [],
                            [],
                            previous_error,
                        )
                        responses.append(response)
                        detail["structured_output"] = True
                        detail["generated_sql"] = response.content
                        detail["attempts"] = attempt + 1
                        try:
                            sqlite_sql = _to_sqlite(response.content)
                            detail["ast_valid"] = True
                            candidate_rows = connection.execute(sqlite_sql).fetchall()
                            detail["execution_success"] = True
                            break
                        except Exception as exc:  # noqa: BLE001 - 与 Runtime 一致，仅纠错一次
                            if attempt == 1:
                                raise
                            previous_error = f"{type(exc).__name__}：执行失败"
                    expected_rows = _execute(connection, case.reference_sql or "")
                    # 以参考 SQL 的顶层 ORDER BY 判断顺序是否属于题目要求。
                    ordered = parse_one(case.reference_sql or "", read="mysql").args.get("order") is not None
                    detail["result_correct"] = canonical_rows(candidate_rows, ordered=ordered) == canonical_rows(
                        expected_rows, ordered=ordered
                    )
                    detail["candidate_row_count"] = len(candidate_rows)
                else:
                    sources = await retriever.retrieve(case.query, top_k=3)
                    response = await adapter.answer(case.query, "knowledge", [], sources, None, [])
                    lowered = response.content.lower()
                    detail["keyword_coverage"] = all(
                        keyword.lower() in lowered for keyword in case.expected_keywords
                    )
                    detail["citation_complete"] = any(
                        source.filename.lower() in lowered for source in sources
                    )
                    detail["source_count"] = len(sources)
                    detail["answer_preview"] = response.content[:300]
                    responses.append(response)
                for model_response in responses:
                    for key in usage:
                        value = getattr(model_response, key)
                        if value is not None:
                            usage[key] += value
                detail["passed"] = bool(
                    detail.get("result_correct")
                    if case.kind == "sql"
                    else detail.get("keyword_coverage") and detail.get("citation_complete")
                )
            except Exception as exc:  # noqa: BLE001 - 报告只记录错误类别
                detail["passed"] = False
                detail["error_type"] = type(exc).__name__
            latency_ms = round((time.perf_counter() - started) * 1000)
            detail["latency_ms"] = latency_ms
            latencies.append(latency_ms)
            details.append(detail)
            if request_delay_seconds:
                await asyncio.sleep(request_delay_seconds)
    finally:
        connection.close()

    sql_details = [item for item in details if item["kind"] == "sql"]
    answer_details = [item for item in details if item["kind"] == "answer"]

    def rate(items: list[dict[str, Any]], key: str) -> float:
        return round(sum(bool(item.get(key)) for item in items) / len(items) * 100, 2) if items else 0.0

    return {
        "run_at_utc": datetime.now(UTC).isoformat(),
        "dataset": str(DEFAULT_DATASET.name),
        "data_rows": data_rows,
        "summary": {
            "total": len(details),
            "passed": sum(bool(item["passed"]) for item in details),
            "pass_rate_pct": rate(details, "passed"),
            "sql_total": len(sql_details),
            "structured_output_rate_pct": rate(sql_details, "structured_output"),
            "sql_ast_valid_rate_pct": rate(sql_details, "ast_valid"),
            "sql_execution_success_rate_pct": rate(sql_details, "execution_success"),
            "sql_result_accuracy_pct": rate(sql_details, "result_correct"),
            "sql_retry_cases": sum(item.get("attempts", 1) > 1 for item in sql_details),
            "answer_total": len(answer_details),
            "answer_keyword_coverage_pct": rate(answer_details, "keyword_coverage"),
            "citation_complete_rate_pct": rate(answer_details, "citation_complete"),
            "latency_p50_ms": _percentile(latencies, 0.5),
            "latency_p95_ms": _percentile(latencies, 0.95),
            **usage,
        },
        "details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="显式运行真实模型 Agent 评测")
    parser.add_argument("--allow-network", action="store_true", help="确认允许调用外部模型 API")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()

    api_key = os.environ.get("LLM_API_KEY", "")
    if not args.allow_network:
        parser.error("真实模型评测必须显式传入 --allow-network")
    if not api_key:
        parser.error("请通过 LLM_API_KEY 环境变量提供密钥")

    adapter = OpenAIModelAdapter(
        api_key=api_key,
        base_url=os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        model=os.environ.get("LLM_MODEL", "glm-4.7-flash"),
        business_context=_BUSINESS_CONTEXT,
    )
    report = asyncio.run(
        evaluate_live_model(
            adapter=adapter,
            cases=load_live_cases(args.dataset),
            csv_path=args.csv,
            request_delay_seconds=max(0, args.delay),
        )
    )
    report["provider"] = "Zhipu BigModel"
    report["model"] = os.environ.get("LLM_MODEL", "glm-4.7-flash")
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["summary"]["pass_rate_pct"] >= 70 else 1


if __name__ == "__main__":
    raise SystemExit(main())
