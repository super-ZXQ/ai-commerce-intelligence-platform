from collections import Counter
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from agent_core.db_schema import describe_table
from agent_core.live_evaluation import LiveCase, canonical_rows, evaluate_live_model, load_live_cases
from agent_core.model_adapter import OpenAIModelAdapter
from agent_core.models import ModelResponse


def test_live_dataset_has_sql_and_answer_cases() -> None:
    cases = load_live_cases()
    assert len(cases) == 15
    assert Counter(case.kind for case in cases) == {"sql": 10, "answer": 5}


def test_live_dataset_rejects_duplicate_ids(tmp_path: Path) -> None:
    row = '{"id":"same","kind":"answer","query":"问题","expected_keywords":["答案"]}'
    path = tmp_path / "duplicate.jsonl"
    path.write_text(f"{row}\n{row}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="id 必须唯一"):
        load_live_cases(path)


def test_canonical_rows_preserves_column_order_and_respects_row_order() -> None:
    expected = [("APP", 10.001), ("Web", 20)]
    actual = [("Web", 20.0), ("APP", 10)]
    assert canonical_rows(actual) == canonical_rows(expected)
    assert canonical_rows(actual, ordered=True) != canonical_rows(expected, ordered=True)
    assert canonical_rows([(10, "APP"), (20, "Web")]) != canonical_rows(expected)


@pytest.mark.asyncio
async def test_live_evaluation_rejects_wrong_sort_order(monkeypatch, tmp_path: Path) -> None:
    """通过完整评测流程检查排序错误不会被判为正确。"""
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE orders (platform_type TEXT, payment_amount REAL)")
    connection.executemany("INSERT INTO orders VALUES (?, ?)", [("APP", 10), ("Web", 20)])
    monkeypatch.setattr("agent_core.live_evaluation.build_sqlite_database", lambda _: connection)

    class Adapter:
        async def generate_sql(self, *_args):
            return ModelResponse("SELECT platform_type, payment_amount FROM orders ORDER BY payment_amount ASC")

    case = LiveCase(
        id="sort", kind="sql", query="按金额降序",
        reference_sql="SELECT platform_type, payment_amount FROM orders ORDER BY payment_amount DESC",
    )
    report = await evaluate_live_model(adapter=Adapter(), cases=[case], csv_path=tmp_path / "unused", request_delay_seconds=0)
    assert report["details"][0]["execution_success"] is True
    assert report["details"][0]["result_correct"] is False


def test_describe_table_uses_sqlalchemy_inspection() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE orders (order_id TEXT NOT NULL, amount REAL)"))
    schema = describe_table(engine, "orders")
    assert "CREATE TABLE orders" in schema
    assert "order_id TEXT NOT NULL" in schema
    assert "amount REAL" in schema


@pytest.mark.asyncio
async def test_model_adapter_explicitly_uses_json_mode(monkeypatch) -> None:
    captured = {}

    class FakeStructuredModel:
        async def ainvoke(self, prompt: str):
            captured["prompt"] = prompt
            return {
                "parsed": SimpleNamespace(sql="SELECT COUNT(*) FROM orders"),
                "raw": SimpleNamespace(content="", usage_metadata={"total_tokens": 5}),
            }

    class FakeClient:
        def with_structured_output(self, _schema, **kwargs):
            captured.update(kwargs)
            return FakeStructuredModel()

    adapter = OpenAIModelAdapter(
        api_key="test-only",
        base_url="https://example.invalid",
        model="fake-model",
        business_context="测试口径",
    )
    monkeypatch.setattr(adapter, "_client", FakeClient)
    response = await adapter.generate_sql("订单数", "orders(order_id TEXT)", [], [], None)

    assert captured["method"] == "json_mode"
    assert captured["include_raw"] is True
    assert "必须只输出 JSON 对象" in captured["prompt"]
    assert response.total_tokens == 5


@pytest.mark.asyncio
async def test_live_evaluation_retries_execution_error_once(tmp_path: Path) -> None:
    csv_path = tmp_path / "orders.csv"
    pd.DataFrame(
        [
            {
                "订单顺序编号": 1,
                "订单号": "order-1",
                "用户名": "user-1",
                "商品编号": "product-1",
                "订单金额": 10,
                "付款金额": 10,
                "渠道编号": "channel-1",
                "平台类型": "APP",
                "下单时间": "2025-01-01 10:00:00",
                "付款时间": "2025-01-01 10:00:01",
                "是否退款": "否",
                "优惠金额": 0,
                "支付耗时_秒": 1,
                "下单日期": "2025-01-01",
                "下单小时": 10,
                "星期几": "Wednesday",
            }
        ]
    ).to_csv(csv_path, index=False)

    class FakeAdapter:
        def __init__(self):
            self.calls = 0

        async def generate_sql(self, *_args):
            self.calls += 1
            sql = (
                "SELECT SUM(payment_amount)"
                if self.calls == 1
                else "SELECT SUM(payment_amount) FROM orders"
            )
            return ModelResponse(sql, total_tokens=3)

    adapter = FakeAdapter()
    report = await evaluate_live_model(
        adapter=adapter,
        cases=[
            LiveCase(
                id="retry",
                kind="sql",
                query="销售额",
                reference_sql="SELECT SUM(payment_amount) FROM orders",
            )
        ],
        csv_path=csv_path,
        request_delay_seconds=0,
    )

    assert adapter.calls == 2
    assert report["details"][0]["attempts"] == 2
    assert report["details"][0]["passed"] is True
    assert report["summary"]["total_tokens"] == 6
