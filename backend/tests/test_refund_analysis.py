"""退款率多步分析：真实 SQLite 结果、边界及工具次数。"""
from __future__ import annotations

import sqlite3
import os
from collections import deque
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_core.models import ModelResponse
from agent_core.model_adapter import OpenAIModelAdapter
from agent_core.refund_analysis import supports_refund_analysis
from agent_core.refund_analysis import refund_sql
from agent_core.runtime import AgentRuntime
from agent_core.sql_safety import apply_execution_guard, validate_and_limit_sql


def runtime_for_test(choices, *, latest_refund=True, fail_on=None):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE orders (order_id TEXT, order_date TEXT, is_refund TEXT, platform_type TEXT, product_id TEXT)")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", [
        ("p1", "2025-01-01", "否", "APP", "A"),
        ("p2", "2025-01-30", "否", "APP", "B"),
        ("c1", "2025-01-31", "是" if latest_refund else "否", "APP", "A"),
        ("c2", "2025-03-01", "否", "Web", "B"),
    ])
    selected = deque(choices)
    calls = []
    answers = []

    async def retrieve(_query):
        return []

    async def schema():
        return "orders(order_id, order_date, is_refund, platform_type, product_id)"

    async def generate(*_args):
        return ModelResponse("SELECT COUNT(*) FROM orders")

    async def execute(sql):
        calls.append(sql)
        if fail_on == len(calls):
            raise RuntimeError("数据库连接明文不应泄露")
        return [dict(row) for row in db.execute(sql).fetchall()]

    async def choose(_query, evidence):
        assert len(evidence) == len(calls)
        return ModelResponse(selected.popleft(), total_tokens=10)

    async def answer(_query, _intent, _history, _sources, _sql, rows):
        answers.append(rows)
        return ModelResponse("分析完成", total_tokens=2)

    runtime = AgentRuntime(
        retriever=retrieve, schema_loader=schema, sql_generator=generate,
        sql_executor=execute, answer_generator=answer, analysis_planner=choose,
        sql_dialect="sqlite",
    )
    return runtime, calls, answers, db


@pytest.mark.asyncio
async def test_multistep_analysis_executes_three_aggregate_queries_and_streams_steps():
    runtime, calls, answers, db = runtime_for_test(["platform", "product"])
    events = []
    state = await runtime.astream("最近退款率为什么上升？", on_step=events.append)
    evidence = state["result"].analysis_evidence
    assert [item["tool"] for item in evidence] == ["overall", "platform", "product"]
    assert len(calls) == 3
    overall = evidence[0]["rows"][0]
    assert (overall["previous_start"], overall["previous_end"]) == ("2025-01-01", "2025-01-30")
    assert (overall["current_start"], overall["current_end"]) == ("2025-01-31", "2025-03-01")
    assert (overall["previous_orders"], overall["previous_refunds"]) == (2, 0)
    assert (overall["current_orders"], overall["current_refunds"]) == (2, 1)
    assert answers == [evidence]
    assert state["result"].usage.total_tokens == 22
    assert sum(event["name"] == "analysis_execute" for event in events) == 3
    assert "不能证明退款原因" in state["answer"]
    db.close()


@pytest.mark.asyncio
async def test_no_increase_stops_without_planner_or_model_answer():
    runtime, calls, answers, db = runtime_for_test([], latest_refund=False)
    state = await runtime.invoke("退款率为什么上升？")
    assert len(calls) == 1 and not answers
    assert "没有显示退款率上升" in state["answer"]
    db.close()


@pytest.mark.asyncio
async def test_empty_data_stops_and_reports_insufficient_evidence():
    runtime, calls, answers, db = runtime_for_test([])
    db.execute("DELETE FROM orders")
    state = await runtime.invoke("退款率为什么上升？")
    assert len(calls) == 1 and not answers
    assert "没有订单数据" in state["answer"]
    db.close()


@pytest.mark.asyncio
async def test_execution_failure_stops_and_retains_previous_evidence():
    runtime, calls, answers, db = runtime_for_test(["platform"], fail_on=2)
    state = await runtime.invoke("退款率为什么上升？")
    assert len(calls) == 2 and not answers
    assert len(state["result"].analysis_evidence) == 1
    assert state["result"].sql_error and "数据库连接明文" not in state["answer"]
    db.close()


@pytest.mark.asyncio
async def test_duplicate_tool_is_rejected_before_execution():
    runtime, calls, _answers, db = runtime_for_test(["platform", "platform"])
    state = await runtime.invoke("退款率为什么上升？")
    assert len(calls) == 2
    assert state["result"].sql_error
    db.close()


def test_explicit_time_or_other_questions_use_existing_path():
    assert supports_refund_analysis("退款率为什么上升")
    assert not supports_refund_analysis("2025年退款率为什么上升")
    assert not supports_refund_analysis("最近三个月退款率为什么上升")
    assert not supports_refund_analysis("退款率是多少")


@pytest.mark.asyncio
async def test_planner_uses_structured_output_and_preserves_token_usage(monkeypatch):
    captured = {}

    class StructuredClient:
        async def ainvoke(self, prompt):
            captured["prompt"] = prompt
            return {"parsed": SimpleNamespace(action="platform"),
                    "raw": SimpleNamespace(content="", usage_metadata={"total_tokens": 7})}

    class Client:
        def with_structured_output(self, _schema, **kwargs):
            captured.update(kwargs)
            return StructuredClient()

    adapter = OpenAIModelAdapter(api_key="test", base_url="https://example.invalid", model="fake", business_context="")
    monkeypatch.setattr(adapter, "_client", Client)
    response = await adapter.choose_refund_analysis("退款率为什么上升", [{"tool": "overall", "rows": []}])
    assert response.content == "platform" and response.total_tokens == 7
    assert captured["method"] == "json_mode"
    assert "overall" in captured["prompt"]
    assert "用户明确要求按平台或商品分析时" in captured["prompt"]


def test_refund_tools_on_isolated_mysql():
    """仅在显式隔离的 MySQL 实例创建临时库，验证实际 MySQL 方言。"""
    if os.environ.get("TEST_ORDER_CONCURRENCY") != "1":
        pytest.skip("仅在隔离 MySQL 测试实例运行")
    import pymysql

    from backend.config import get_settings

    settings = get_settings()
    database = f"refund_eval_{uuid4().hex}"
    conn = pymysql.connect(host=settings.db_host, port=settings.db_port,
                           user=settings.db_user, password=settings.db_password, autocommit=True)
    created = False
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
            created = True
            cursor.execute(f"USE `{database}`")
            cursor.execute("CREATE TABLE orders (order_id VARCHAR(50), order_date DATE, is_refund VARCHAR(10), "
                           "platform_type VARCHAR(50), product_id VARCHAR(50))")
            cursor.executemany("INSERT INTO orders VALUES (%s, %s, %s, %s, %s)", [
                ("p1", "2025-01-01", "否", "APP", "A"),
                ("p2", "2025-01-30", "否", "Web", "B"),
                ("c1", "2025-01-31", "是", "APP", "A"),
                ("c2", "2025-03-01", "否", "Web", "B"),
            ])
            for tool in ("overall", "platform", "product"):
                sql = apply_execution_guard(validate_and_limit_sql(refund_sql(tool, "mysql"), max_rows=50), 10000)
                cursor.execute(sql)
                rows = cursor.fetchall()
                assert rows, tool
                if tool == "overall":
                    assert tuple(map(str, rows[0][:4])) == ("2025-03-01", "2025-01-31", "2025-01-01", "2025-01-30")
                    assert tuple(rows[0][4:]) == (2, 1, 2, 0)
    finally:
        if created:
            with conn.cursor() as cursor:
                cursor.execute(f"DROP DATABASE `{database}`")
        conn.close()
