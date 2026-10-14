"""评测器统计契约与oracle安全检查，不调用模型。"""
import pytest

from agent_core.eval.audit_experiments import cases, rag_summary, result_equal, route_summary, sql_summary_provenance
from agent_core.sql_safety import validate_and_limit_sql


def test_independent_dataset_sizes_and_reference_sql_policy():
    assert len(cases("audit_routing.jsonl")) == 30
    assert len(cases("audit_rag.jsonl")) == 18
    sql_cases = cases("audit_sql.jsonl")
    assert len(sql_cases) == 20
    for item in sql_cases:
        if item.get("reference_sql"):
            assert validate_and_limit_sql(item["reference_sql"])


def test_result_comparison_preserves_column_identity_and_rank():
    assert result_equal([("APP", 10.001)], [("APP", 10)])
    assert not result_equal([(10, "APP")], [("APP", 10)])
    assert not result_equal([(10, 20)], [(20, 10)])
    assert not result_equal([("B", 20), ("A", 10)], [("A", 10), ("B", 20)])
    assert not result_equal([(None,)], [(0,)])


def test_provider_failure_is_not_fabricated_model_accuracy_or_free_tokens():
    result = route_summary([{"gold": "data", "pred": "error", "model_called": True, "error": "AuthenticationError", "latency_ms": 10}])
    assert result["status"] == "PROVIDER_FAILED"
    assert result["accuracy"] is result["macro_f1"] is result["input_tokens"] is None
    result = sql_summary_provenance({"details": [{"attempts": 1, "execution_success": False, "error": "AuthenticationError"}]})
    assert result["result_accuracy"] is result["mean_tokens"] is None


def test_rag_metrics_do_not_count_no_answer_as_recall_hit():
    result = rag_summary([
        {"id": "positive", "no_answer": False, "hit": True, "rr": .5, "refused": False, "latency_ms": 1},
        {"id": "negative", "no_answer": True, "hit": False, "rr": 0, "refused": True, "latency_ms": 1},
    ])
    assert result["recall_at_3"] == result["no_answer_accuracy"] == 1
    assert result["mrr"] == .5


@pytest.mark.parametrize("grants", [
    ["GRANT ALL PRIVILEGES ON *.* TO 'ai'@'%'"],
    ["GRANT SELECT ON `shop`.* TO 'ai'@'%'"],
    ["GRANT SELECT ON `shop`.`orders` TO 'ai'@'%' WITH GRANT OPTION"],
    ["GRANT `writer`@`%` TO `ai`@`%`"],
    ["GRANT SELECT (`email`) ON `shop`.`orders` TO 'ai'@'%'"],
])
def test_actual_grants_policy_rejects_privilege_expansion(grants):
    from types import SimpleNamespace
    from agent_core.db_permissions import verify_ai_permissions

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, *a):
            return [(g,) for g in grants]

    with pytest.raises(ValueError):
        verify_ai_permissions(SimpleNamespace(url=SimpleNamespace(database="shop"), connect=Connection))
