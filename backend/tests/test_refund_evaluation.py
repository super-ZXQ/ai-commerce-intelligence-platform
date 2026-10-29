"""评测框架的回归测试：自动检查与模型回答质量分开。"""
import json
from pathlib import Path

import pytest

from agent_core.models import ModelResponse
from agent_core.refund_evaluation import CASES, evaluate, evaluate_case


@pytest.mark.asyncio
async def test_offline_evaluation_covers_all_cases():
    report = await evaluate()
    assert report["mode"] == "scripted_offline"
    assert report["total"] == 6
    assert report["automatic_checks_passed"] == 6
    assert all(item["answer_quality"] == "未评估模型回答" for item in report["cases"])


@pytest.mark.asyncio
async def test_holdout_cases_have_unique_ids_and_pass_offline_checks():
    path = Path(__file__).resolve().parents[2] / "agent_core/eval/refund_holdout.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(cases) == len({case["id"] for case in cases}) == 21
    report = await evaluate(cases=cases)
    assert report["automatic_checks_passed"] == report["total"] == 21


@pytest.mark.asyncio
async def test_model_stopping_before_required_tool_fails_automatic_check():
    class FakeModel:
        async def choose_refund_analysis(self, _query, _evidence):
            return ModelResponse("finish")

        async def answer(self, *_args):
            return ModelResponse("没有可核验的结论")

    result = await evaluate_case(CASES[0], FakeModel())
    assert result["checks"]["expected_tools"] is False
    assert result["automatic_checks_passed"] is False
    assert result["answer_quality"] == "需人工核验"


@pytest.mark.asyncio
async def test_model_answer_failure_fails_automatic_check():
    class FakeModel:
        async def choose_refund_analysis(self, _query, evidence):
            return ModelResponse("platform" if len(evidence) == 1 else "finish")

        async def answer(self, *_args):
            raise RuntimeError("模拟模型服务失败")

    result = await evaluate_case(CASES[0], FakeModel())
    assert result["checks"]["expected_tools"] is True
    assert result["checks"]["answer_generated"] is False
    assert result["automatic_checks_passed"] is False
