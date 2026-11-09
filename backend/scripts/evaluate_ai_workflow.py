"""用独立 MySQL 库的合成订单评测 FastAPI 流式问答。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, func

from agent_core.live_evaluation import canonical_rows
from backend.config import get_settings
from backend.models.database_models import AuthUser, Order

_DATASET = Path(__file__).resolve().parents[2] / "agent_core/eval/ai_workflow_mysql.jsonl"
_DATABASE = "ai_workflow_eval"
_COUNTS = ("current_orders", "current_refunds", "previous_orders", "previous_refunds")


def _fixture_orders() -> list[dict]:
    """两窗口各 60 单，覆盖退款上升、60 个商品分组和更晚的取消订单。"""
    orders = []
    for period, day in enumerate((date(2025, 3, 1), date(2025, 4, 29))):
        for index in range(1, 61):
            app = index <= 40
            refunded = index in (41, 42) or (index <= 16 if period else 2 <= index <= 5)
            amount = (100 if app else 200) + 20 * period
            sku = 1 if period and index <= 3 else index
            orders.append({
                "order_id": f"EVAL-{period}-{index:03d}",
                "user_name": f"合成客户-{index:03d}",
                "product_id": f"SKU-{sku:03d}",
                "platform_type": "APP" if app else "Web网站",
                "order_amount": amount, "payment_amount": amount,
                "order_date": day, "order_time": datetime.combine(day, datetime.min.time()),
                "is_refund": "是" if refunded else "否",
                "order_status": "REFUNDED" if refunded else "ACTIVE",
            })
    orders.append({
        **orders[-1], "order_id": "EVAL-CANCELLED", "order_status": "CANCELLED",
        "order_date": date(2025, 6, 1), "order_time": datetime(2025, 6, 1),
        "order_amount": 9999, "payment_amount": 9999,
    })
    return orders


def _seed_orders(database_url: str) -> int:
    engine = create_engine(database_url)
    orders = _fixture_orders()
    try:
        with engine.begin() as connection:
            for table in (Order.__table__, AuthUser.__table__):
                if connection.scalar(select(func.count()).select_from(table)):
                    raise SystemExit("评测需要刚完成迁移的空隔离库，请重新创建临时 MySQL 实例。")
            connection.execute(Order.__table__.insert(), orders)
    finally:
        engine.dispose()
    return len(orders)


def _answer_checks(case: dict, result: dict) -> dict[str, bool]:
    answer = unicodedata.normalize("NFKC", result.get("answer", ""))
    # SQL 代码块里的数字不能代替回答正文中的数字。
    answer = re.sub(r"```.*?```", "", answer, flags=re.DOTALL)
    plain = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", answer)
    numbers = {Decimal(value) for value in re.findall(r"\d+(?:\.\d+)?", plain)}
    checks = {
        "answer_available": bool(answer) and not answer.startswith("⚠️ 查询执行失败")
        and all(step["status"] != "error" for step in result.get("steps", []) if step["name"] == "synthesize"),
        "answer_numbers": all(Decimal(str(number)) in numbers for number in case.get("answer_numbers", [])),
        "answer_patterns": all(re.search(pattern, answer) for pattern in case.get("answer_patterns", [])),
    }
    if case.get("citation"):
        checks["answer_cites_retrieved_source"] = any(
            source["filename"] in answer for source in result.get("sources", [])
        )
    return checks


def _score(case: dict, result: dict) -> dict:
    steps = result.get("steps", [])
    successful = {step["name"] for step in steps if step["status"] == "success"}
    evidence = result.get("analysis_evidence", [])
    tool_checks = {
        "intent": result.get("intent") == case["intent"],
        "sql_error_free": not result.get("sql_error"),
    }
    if "rows" in case:
        tool_checks["sql_executed"] = "execute_sql" in successful
    if case.get("citation"):
        tool_checks["retrieved"] = "retrieve" in successful and bool(result.get("sources"))
    if case["intent"] in {"blocked", "clarification"}:
        tool_checks["safe_response_only"] = "safe_response" in successful and not successful & {
            "execute_sql", "analysis_execute", "synthesize", "generate_sql", "retrieve",
        }
    data_checks = {}
    if "rows" in case:
        rows = [tuple(row.values()) for row in result.get("result", [])]
        data_checks["result_matches_gold"] = canonical_rows(rows, ordered=True) == canonical_rows(
            case["rows"], ordered=True,
        )
    if "tools" in case:
        tool_checks["refund_tools"] = sorted(entry["tool"] for entry in evidence) == sorted(case["tools"])
        by_tool = {entry["tool"]: entry["rows"] for entry in evidence}
        overall = (by_tool.get("overall") or [{}])[0]
        product = by_tool.get("product", [])
        data_checks["overall_refund_counts"] = [overall.get(key) for key in _COUNTS] == case["refund_counts"]
        data_checks["refund_group_data"] = (
            len(by_tool.get("platform", [])) == 2 and len(product) == 50
            and product[0]["product_id"] == "SKU-001"
            and [product[0][key] for key in _COUNTS] == [3, 3, 1, 0]
        )
    return {"tools": tool_checks, "data": data_checks, "answer": _answer_checks(case, result)}


async def _run(cases: list[dict], username: str, password: str) -> list[dict]:
    from backend.main import app

    report = []
    threads: dict[str, str] = {}
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://ai-workflow-eval.local",
    ) as client:
        login = await client.post("/api/auth/login", json={"username": username, "password": password})
        login.raise_for_status()
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        overview = await client.get("/api/analytics/sales-overview")
        overview.raise_for_status()
        if overview.json()["total_orders"] != 120 or Decimal(str(overview.json()["total_sales"])) != 17200:
            raise SystemExit("隔离库销售总览与合成订单金标不一致，已停止评测。")

        for case in cases:
            thread_key = case.get("thread", case["id"])
            response = await client.post("/api/ai/stream", json={
                "query": case["query"], "thread_id": threads.get(thread_key),
            })
            events = [json.loads(line) for line in response.text.splitlines() if line] if response.is_success else []
            result = next((event["response"] for event in events if event["type"] == "final"), {})
            if result:
                threads[thread_key] = result["thread_id"]
            checks = _score(case, result)
            checks["tools"]["http_final"] = response.is_success and bool(result)
            report.append({
                "id": case["id"], "query": case["query"], "http_status": response.status_code,
                "checks": checks,
                **{key: result.get(key) for key in (
                    "intent", "answer", "sql", "result", "sources", "steps", "usage", "sql_error", "analysis_evidence",
                )},
            })
            print(case["id"], {name: all(items.values()) for name, items in checks.items() if items}, flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-network", action="store_true", help="显式允许使用配置的真实模型")
    parser.add_argument("--output", type=Path, default=Path("output/ai-workflow-mysql.json"))
    args = parser.parse_args()
    if not args.allow_network:
        parser.error("真实评测需要 --allow-network")
    settings = get_settings()
    if settings.db_host != "127.0.0.1" or settings.db_name != _DATABASE:
        parser.error(f"评测必须连接 127.0.0.1 上的专用库 {_DATABASE}")
    if not settings.ai_db_user or settings.redis_enabled or settings.agent_episode_vector_enabled:
        parser.error("评测需要 AI 只读账号，并关闭 Redis 和情节向量")
    if not settings.llm_api_key or settings.llm_mode != "managed":
        parser.error("评测需要已配置模型密钥的 managed 模式")

    dataset = _DATASET.read_bytes()
    cases = [json.loads(line) for line in dataset.splitlines() if line]
    seed_rows = _seed_orders(settings.database_url)
    details = asyncio.run(_run(cases, settings.admin_username, settings.admin_password))
    summary = {}
    for name in ("tools", "data", "answer"):
        scores = [item["checks"][name] for item in details if item["checks"][name]]
        summary[name] = {"passed": sum(all(score.values()) for score in scores), "total": len(scores)}
    report = {
        "run_at_utc": datetime.now(UTC).isoformat(), "model": settings.llm_model,
        "fixture": {"rows": seed_rows, "kind": "synthetic_mysql", "endpoint": "/api/ai/stream"},
        "dataset_sha256": hashlib.sha256(dataset).hexdigest(), "summary": summary, "cases": details,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(summary)
    return int(any(score["passed"] < score["total"] for score in summary.values()))


if __name__ == "__main__":
    raise SystemExit(main())
