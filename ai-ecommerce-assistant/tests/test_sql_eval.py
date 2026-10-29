"""Text-to-SQL 评估核心逻辑测试（不依赖 LLM API Key，CI 可跑）。

覆盖 run_sql_eval.py 的纯函数部分：
- normalize_sql / check_sql_keywords 的归一化匹配语义
- gold SQL 能通过生产同一道 validate_and_limit_sql 闸门
- load_data_cases 只加载 data 类且强制要求 expected_sql_keywords
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# eval/ 不是包，按文件位置直接加到 sys.path
EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from run_sql_eval import check_sql_keywords, evaluate_case, load_data_cases, normalize_sql  # noqa: E402
from agent_core.sql_safety import validate_and_limit_sql  # noqa: E402
from agent_core.models import ModelResponse  # noqa: E402


GOLD_SQL = {
    "d01": 'SELECT SUM(payment_amount) AS total_sales FROM orders WHERE order_date >= \'2025-12-25\'',
    "d02": 'SELECT platform_type, COUNT(DISTINCT order_id) AS cnt FROM orders WHERE platform_type IN (\'APP\', \'微信公众号\') GROUP BY platform_type ORDER BY cnt DESC',
    "d03": 'SELECT product_id, SUM(CASE WHEN is_refund = \'是\' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS refund_rate FROM orders GROUP BY product_id ORDER BY refund_rate DESC LIMIT 3',
    "d06": 'SELECT COUNT(DISTINCT CASE WHEN order_count >= 2 THEN user_name END) * 1.0 / COUNT(DISTINCT user_name) AS repurchase_rate FROM (SELECT user_name, COUNT(DISTINCT order_id) AS order_count FROM orders GROUP BY user_name) t',
}


def test_normalize_sql_folds_case_whitespace_and_backticks():
    assert normalize_sql("select  sum( `payment_amount` );") == "SELECT SUM(PAYMENT_AMOUNT)"


def test_check_sql_keywords_all_hit():
    sql = "SELECT SUM(payment_amount) FROM orders WHERE order_date >= '2025-12-25'"
    assert check_sql_keywords(sql, ["SUM(payment_amount)", "order_date"]) == []


def test_check_sql_keywords_reports_missing():
    sql = "SELECT COUNT(*) FROM orders GROUP BY platform"
    missing = check_sql_keywords(sql, ["SUM(payment_amount)", "platform"])
    assert missing == ["SUM(payment_amount)"]


def test_check_sql_keywords_tolerates_spacing():
    """gold 写 COUNT(DISTINCT，模型写 COUNT(DISTINCT user_id)（多空格/换行）也应命中。"""
    sql = "SELECT COUNT(DISTINCT  user_id) FROM orders"
    assert check_sql_keywords(sql, ["COUNT(DISTINCT"]) == []


def test_gold_sql_passes_production_ast_gate():
    """gold SQL 必须能通过生产 validate_and_limit_sql，否则评估口径与生产脱节。"""
    for case_id, sql in GOLD_SQL.items():
        validated = validate_and_limit_sql(sql)
        assert validated, case_id


def test_gold_sql_keywords_hit():
    """gold SQL 应命中自身声明的关键词（验证 check_sql_keywords 的匹配口径）。"""
    expected = {
        "d01": ["SUM(payment_amount)", "order_date"],
        "d02": ["platform", "COUNT", "ORDER BY"],
        "d03": ["is_refund", "GROUP BY", "LIMIT 3"],
        "d06": ["COUNT(DISTINCT", "user_name"],
    }
    for case_id, keywords in expected.items():
        assert check_sql_keywords(GOLD_SQL[case_id], keywords) == [], case_id


@pytest.mark.asyncio
async def test_execution_failure_does_not_pass_keyword_check(monkeypatch):
    """执行失败时，关键词全部命中也不能通过评测。"""
    class Adapter:
        async def generate_sql(self, *args, **kwargs):
            return ModelResponse("SELECT SUM(payment_amount) FROM orders")

    monkeypatch.setattr("run_sql_eval.execute_sql", lambda _: {"executed": False, "exec_error": "失败"})
    case = {"id": "d", "question": "销售额", "expected_sql_keywords": ["SUM(payment_amount)"]}
    result = await evaluate_case(Adapter(), "orders(payment_amount DECIMAL)", case, True)
    assert result["keyword_ok"] is True
    assert result["executed"] is False
    assert result["passed"] is False


def test_load_data_cases_filters_knowledge_and_requires_keywords(tmp_path: Path):
    dataset = tmp_path / "gold_qa.jsonl"
    dataset.write_text(
        "\n".join([
            json.dumps({"id": "k01", "category": "knowledge", "question": "q"}, ensure_ascii=False),
            json.dumps({"id": "d01", "category": "data", "question": "q1",
                        "expected_sql_keywords": ["SUM(payment_amount)"]}, ensure_ascii=False),
        ]),
        encoding="utf-8",
    )
    cases = load_data_cases(dataset)
    assert [c["id"] for c in cases] == ["d01"]


def test_load_data_cases_rejects_data_case_without_keywords(tmp_path: Path):
    dataset = tmp_path / "gold_qa.jsonl"
    dataset.write_text(
        json.dumps({"id": "d11", "category": "data", "question": "q"}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="expected_sql_keywords"):
        load_data_cases(dataset)
