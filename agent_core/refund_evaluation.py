"""退款率多步分析的合成数据评测；真实模型模式需显式允许联网。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from agent_core.model_adapter import OpenAIModelAdapter
from agent_core.models import ModelResponse
from agent_core.runtime import AgentRuntime

# 所有数据均为人工构造；期望值独立写明，避免用待测 SQL 计算参考答案。
CASES = (
    {"id": "rise-platform", "query": "退款率为什么上升？请按平台分析", "data": "rise",
     "choices": ["platform", "finish"], "required": ["overall", "platform"], "counts": [2, 1, 2, 0]},
    {"id": "rise-product", "query": "退款率为什么上升？请按商品分析", "data": "rise",
     "choices": ["product", "finish"], "required": ["overall", "product"], "counts": [2, 1, 2, 0]},
    {"id": "rise-both", "query": "退款率为什么上升？请分析平台和商品", "data": "rise",
     "choices": ["platform", "product"], "required": ["overall", "platform", "product"], "counts": [2, 1, 2, 0]},
    {"id": "no-increase", "query": "退款率为什么上升？", "data": "flat",
     "choices": [], "required": ["overall"], "counts": [2, 0, 2, 0], "answer_contains": "没有显示退款率上升"},
    {"id": "empty", "query": "退款率为什么上升？", "data": "empty",
     "choices": [], "required": ["overall"], "counts": None, "answer_contains": "没有订单数据"},
    {"id": "db-error", "query": "退款率为什么上升？", "data": "rise", "fail_first": True,
     "choices": [], "required": [], "counts": None, "error": True},
)


def _database(kind: str) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE orders (order_id TEXT, order_date TEXT, is_refund TEXT, "
        "platform_type TEXT, product_id TEXT, order_status TEXT)"
    )
    if kind in ("holdout_rise", "holdout_flat"):
        # 留出集使用另一批合成订单，避免复用开发集中的 2 单对比。
        db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", [
            ("old-1", "2025-07-02", "是" if kind == "holdout_flat" else "否", "网页", "X", "ACTIVE"),
            ("old-2", "2025-07-31", "是" if kind == "holdout_flat" else "否", "小程序", "Y", "ACTIVE"),
            ("old-3", "2025-07-15", "否", "网页", "Y", "ACTIVE"),
            ("new-1", "2025-08-01", "是", "网页", "X", "REFUNDED"),
            ("new-2", "2025-08-30", "是" if kind == "holdout_rise" else "否", "小程序", "Z", "ACTIVE"),
            ("new-3", "2025-08-15", "否", "网页", "Z", "ACTIVE"),
        ])
    elif kind != "empty":
        db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", [
            ("p1", "2025-01-01", "否", "APP", "A", "ACTIVE"),
            ("p2", "2025-01-30", "否", "Web", "B", "ACTIVE"),
            ("c1", "2025-01-31", "是" if kind == "rise" else "否", "APP", "A", "ACTIVE"),
            ("c2", "2025-03-01", "否", "Web", "B", "ACTIVE"),
        ])
    return db


async def evaluate_case(case: dict, adapter: OpenAIModelAdapter | None = None) -> dict:
    """运行实际 Runtime 与 SQLite；模型模式下回答仍需人工核验。"""
    db = _database(case["data"])
    choices = deque(case["choices"])
    query_attempts = 0

    async def retrieve(_query):
        return []

    async def schema():
        return "orders(order_id, order_date, is_refund, platform_type, product_id)"

    async def generate(*_args):
        raise RuntimeError("本评测仅覆盖退款率多步分析")

    async def execute(sql):
        nonlocal query_attempts
        query_attempts += 1
        if case.get("fail_first") and query_attempts == 1:
            raise RuntimeError("模拟数据库失败")
        return [dict(row) for row in db.execute(sql).fetchall()]

    async def choose(query, evidence):
        if adapter is not None:
            return await adapter.choose_refund_analysis(query, evidence)
        return ModelResponse(choices.popleft())

    async def answer(query, intent, history, sources, sql, rows):
        if adapter is not None:
            return await adapter.answer(query, intent, history, sources, sql, rows)
        return ModelResponse("离线占位回答：不用于评估模型回答质量。")

    runtime = AgentRuntime(
        retriever=retrieve, schema_loader=schema, sql_generator=generate, sql_executor=execute,
        answer_generator=answer, analysis_planner=choose, sql_dialect="sqlite",
    )
    try:
        state = await runtime.invoke(case["query"])
    finally:
        db.close()
    result = state["result"]
    evidence = result.analysis_evidence
    tools = [entry["tool"] for entry in evidence]
    overall = evidence[0]["rows"] if evidence else []
    counts = ([overall[0][key] for key in ("current_orders", "current_refunds", "previous_orders", "previous_refunds")]
              if overall else None)
    checks = {
        "expected_tools": set(case["required"]) <= set(tools),
        "no_extra_tools_on_stop": not (case["data"] in ("flat", "holdout_flat", "empty") and tools != ["overall"]),
        "counts": counts == case["counts"],
        "query_limit": query_attempts <= 3,
        "expected_error": bool(result.sql_error) == bool(case.get("error")),
        "deterministic_answer": case.get("answer_contains", "") in result.answer,
        # SQL 成功不代表模型回答也成功；合成节点出错时不能算通过。
        "answer_generated": all(step.status != "error" for step in result.steps if step.name == "synthesize")
        if not case.get("error") else True,
    }
    return {
        "id": case["id"], "query": case["query"], "tools": tools, "query_attempts": query_attempts,
        "counts": counts, "checks": checks, "automatic_checks_passed": all(checks.values()),
        "answer": result.answer, "sql_error": result.sql_error,
        "token_usage": result.usage.total_tokens if result.usage else None,
        "latency_ms": result.usage.latency_ms if result.usage else None,
        "evidence": evidence,
        "steps": [{"name": step.name, "status": step.status, "duration_ms": step.duration_ms,
                   "summary": step.summary} for step in result.steps],
        "answer_quality": "需人工核验" if adapter and case["data"] in ("rise", "holdout_rise") and not case.get("error") else "未评估模型回答",
    }


async def evaluate(adapter: OpenAIModelAdapter | None = None, cases=CASES) -> dict:
    results = [await evaluate_case(case, adapter) for case in cases]
    return {
        "run_at_utc": datetime.now(UTC).isoformat(),
        "mode": "live_model" if adapter else "scripted_offline",
        "data": "人工合成、隔离 SQLite",
        "note": "自动检查覆盖工具、查询次数、分子分母和错误处理；不判断回答的事实完整性或因果表述。",
        "total": len(results),
        "automatic_checks_passed": sum(result["automatic_checks_passed"] for result in results),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="退款率多步分析评测")
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--env-file", type=Path, help="可选：从本地 dotenv 文件加载模型配置")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dataset", type=Path, help="可选：JSONL 格式的独立评测集")
    args = parser.parse_args()
    adapter = None
    if args.mode == "live":
        if not args.allow_network:
            parser.error("真实模型模式需要 --allow-network")
        if args.env_file:
            if not args.env_file.is_file():
                parser.error("指定的模型配置文件不存在")
            from dotenv import load_dotenv

            load_dotenv(args.env_file, override=False)
        if not os.environ.get("LLM_API_KEY"):
            parser.error("真实模型模式需要 LLM_API_KEY")
        adapter = OpenAIModelAdapter(
            api_key=os.environ["LLM_API_KEY"],
            base_url=os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
            model=os.environ.get("LLM_MODEL", "glm-4.7-flash"), business_context="",
        )
    cases = CASES
    if args.dataset:
        cases = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not cases or len({case['id'] for case in cases}) != len(cases):
            parser.error("评测集不能为空且案例 ID 必须唯一")
    report = asyncio.run(evaluate(adapter, cases))
    if args.dataset:
        report["dataset"] = args.dataset.name
    if adapter:
        report["model"] = os.environ.get("LLM_MODEL", "glm-4.7-flash")
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["automatic_checks_passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
