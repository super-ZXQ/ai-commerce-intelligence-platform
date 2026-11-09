"""出域提示词的字符预算与模型输出上限。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_core.context import fit_json_rows
from agent_core.model_adapter import OpenAIModelAdapter

_ROWS = [{"用户名": f"客户{i}", "金额": i} for i in range(20)]


def _adapter(**overrides) -> OpenAIModelAdapter:
    values = dict(
        api_key="test",
        base_url="https://example.invalid",
        model="fake",
        business_context="",
        prompt_rows_max_chars=300,
    )
    values.update(overrides)
    return OpenAIModelAdapter(**values)


def test_fit_json_rows_always_emits_valid_json() -> None:
    """逐行累加而不是切字符串：切在中间会产生非法 JSON，模型会把半截数据当完整数据。"""
    body, kept = fit_json_rows(_ROWS, 300)

    parsed = json.loads(body)
    assert 0 < kept < len(_ROWS)
    assert len(parsed) == kept
    assert len(body) <= 300


def test_fit_json_rows_handles_empty_and_single_huge_row() -> None:
    assert fit_json_rows([], 300) == ("[]", 0)

    huge = [{"备注": "数" * 5000}]
    body, kept = fit_json_rows(huge, 200)

    # 一行都放不下时不能返回空数组：那会让模型回答「查询结果为空」。
    assert kept == 0
    assert "超长单行已截断" in body
    assert body != "[]"


def test_answer_prompt_clips_rows_and_declares_it() -> None:
    prompt = _adapter()._answer_prompt("谁消费最多", "query", [], [], "SELECT 1", _ROWS)
    block = prompt.split("结果：", 1)[1].split("\n", 1)[0]

    # 截断提示必须在下一行，JSON 行本身仍可被模型直接解析。
    assert "仅发送前" in prompt
    assert f"/{len(_ROWS)} 条" in prompt
    assert len(block) <= 300
    parsed = json.loads(block)
    assert 0 < len(parsed) < len(_ROWS)


def test_answer_prompt_never_sends_more_rows_than_the_cap() -> None:
    adapter = _adapter(prompt_rows_max_chars=100_000)
    rows = [{"金额": i} for i in range(200)]

    prompt = adapter._answer_prompt("统计", "query", [], [], "SELECT 1", rows)
    block = prompt.split("结果：", 1)[1].split("\n", 1)[0]

    # 数据库侧 LIMIT 500，提示词侧只带 50 行：行数上限也要有守卫。
    assert len(json.loads(block)) == 50


def test_refund_prompt_keeps_evidence_for_each_tool() -> None:
    counts = {
        "current_start": "2025-12-02", "current_end": "2025-12-31",
        "previous_start": "2025-11-02", "previous_end": "2025-12-01",
        "current_orders": 10145, "current_refunds": 1382,
        "previous_orders": 11018, "previous_refunds": 1472,
    }
    evidence = [
        {"tool": "overall", "sql": "SELECT 整体", "rows": [counts]},
        {"tool": "product", "sql": "SELECT 商品", "rows": [
            {**counts, "product_id": f"SKU-{i:03d}"} for i in range(50)
        ]},
        {"tool": "platform", "sql": "SELECT 平台", "rows": [{**counts, "platform_type": "APP"}]},
    ]
    prompt = _adapter(prompt_rows_max_chars=4000)._answer_prompt("分析退款率", "data", [], [], None, evidence)
    block = prompt.split("结果：", 1)[1].split("\n", 1)[0]
    steps = {step["tool"]: step for step in json.loads(block)}

    assert len(block) <= 4000
    assert steps["overall"]["rows"] == [counts]
    assert steps["platform"]["rows"][0]["platform_type"] == "APP"
    assert steps["product"]["total_rows"] == 50
    assert 0 < len(steps["product"]["rows"]) < 50
    assert steps["product"]["rows"][0] == evidence[1]["rows"][0]
    assert "SELECT 商品" not in block
    assert "请注明拆解样本范围" in prompt
    assert len(evidence[1]["rows"]) == 50


async def test_planner_keeps_the_full_tool_list_even_when_data_is_clipped() -> None:
    """工具清单被裁掉会让规划器重复执行已经跑过的工具。"""
    captured: dict[str, str] = {}

    class StructuredClient:
        async def ainvoke(self, prompt):
            captured["prompt"] = prompt
            return {
                "parsed": SimpleNamespace(action="finish"),
                "raw": SimpleNamespace(content="", usage_metadata=None),
            }

    class Client:
        def with_structured_output(self, _schema, **_kwargs):
            return StructuredClient()

    adapter = _adapter(prompt_rows_max_chars=120)
    adapter._client = Client  # type: ignore[method-assign]
    evidence = [
        {"tool": tool, "rows": [{"平台类型": f"平台{i}", "订单数": i} for i in range(30)]}
        for tool in ("overall", "platform", "product")
    ]
    await adapter.choose_refund_analysis("退款率为什么上升", evidence)

    tools_line = captured["prompt"].split("已执行工具：", 1)[1].split("\n", 1)[0]

    assert json.loads(tools_line) == ["overall", "platform", "product"]
    assert "仅发送前" in captured["prompt"]


def test_max_output_tokens_reaches_the_model_client(monkeypatch) -> None:
    """不设输出上限时，提供方允许生成到模型最大长度，费用和排队时长都按它放大。"""
    seen: dict[str, object] = {}

    class Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr("agent_core.model_adapter.ChatOpenAI", Recorder)
    adapter = _adapter()
    adapter._validated_base_url = lambda: "https://api.deepseek.com"  # type: ignore[method-assign]
    adapter._clients = lambda: (None, None)  # type: ignore[method-assign]

    adapter._client()

    assert seen["max_tokens"] == 1024

    adapter = _adapter(max_output_tokens=256)
    adapter._validated_base_url = lambda: "https://api.deepseek.com"  # type: ignore[method-assign]
    adapter._clients = lambda: (None, None)  # type: ignore[method-assign]
    adapter._client()

    assert seen["max_tokens"] == 256
