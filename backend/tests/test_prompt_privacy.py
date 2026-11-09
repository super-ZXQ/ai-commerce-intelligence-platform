"""结果集出域前的列级假名化：外发提示词只拿到假名，前端仍拿到原值。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_core.model_adapter import OpenAIModelAdapter
from agent_core.models import AgentResult
from agent_core.prompt_privacy import (
    PromptMasker,
    derive_masking_key,
    mask_for_prompt,
    masking_scope,
    pseudonym,
)
from backend.config import _DEV_JWT_PLACEHOLDER, Settings
from backend.services.ai_service import _state_to_response

_KEY = derive_masking_key("a" * 48)
_ROWS = [
    {
        "用户名": "张三",
        "order_id": "SO-2025-0001",
        "手机号": "13800000000",
        "付款金额": 199.5,
        "平台类型": "APP",
    }
]


def _adapter(**overrides) -> OpenAIModelAdapter:
    values = dict(
        api_key="test",
        base_url="https://example.invalid",
        model="fake",
        business_context="",
        masking_key=_KEY,
    )
    values.update(overrides)
    return OpenAIModelAdapter(**values)


def test_pii_columns_are_masked_in_english_and_chinese() -> None:
    masked = mask_for_prompt(_ROWS, masking_key=_KEY)[0]

    assert masked["用户名"] != "张三"
    assert masked["order_id"] != "SO-2025-0001"
    assert masked["手机号"] != "13800000000"
    assert masked["用户名"].startswith("用户#")
    assert masked["order_id"].startswith("订单#")
    assert masked["手机号"].startswith("联系#")


def test_non_pii_values_keep_their_statistics() -> None:
    """金额与渠道等非敏感列必须原样保留，否则模型无法做聚合推理。"""
    masked = mask_for_prompt(_ROWS, masking_key=_KEY)[0]

    assert masked["付款金额"] == 199.5
    assert masked["平台类型"] == "APP"


def test_masking_returns_a_copy_and_leaves_caller_rows_untouched() -> None:
    """同一批行还要返回前端；原始数据被改掉就是数据事故。"""
    rows = [dict(row) for row in _ROWS]
    original = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)

    mask_for_prompt(rows, masking_key=_KEY)

    assert json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str) == original


def test_pseudonyms_are_stable_per_key_and_change_with_the_key() -> None:
    first = mask_for_prompt(_ROWS, masking_key=_KEY)[0]["用户名"]
    again = mask_for_prompt(_ROWS, masking_key=_KEY)[0]["用户名"]
    other_key = mask_for_prompt(_ROWS, masking_key=derive_masking_key("b" * 48))[0]["用户名"]

    assert first == again
    assert other_key != first
    # 同一密钥下同名同假名，模型才能去重分组
    assert pseudonym("用户", "张三", _KEY) == first


def test_missing_key_still_masks_with_an_ephemeral_secret() -> None:
    """调用方漏传密钥也不能退化成明文外发。"""
    masked = mask_for_prompt(_ROWS)[0]

    assert masked["用户名"].startswith("用户#")
    assert masked["用户名"] != "张三"


def test_nested_evidence_masks_inner_rows() -> None:
    evidence = [{"tool": "overall", "rows": [{"客户名称": "李四", "退款单数": 3}]}]

    masked = mask_for_prompt(evidence, masking_key=_KEY)

    assert masked[0]["tool"] == "overall"
    assert masked[0]["rows"][0]["客户名称"] != "李四"
    assert masked[0]["rows"][0]["退款单数"] == 3


def test_answer_prompt_sends_only_pseudonyms() -> None:
    prompt = _adapter()._answer_prompt("谁的订单最多", "query", [], [], "SELECT 1", _ROWS)

    assert "张三" not in prompt
    assert "SO-2025-0001" not in prompt
    assert "13800000000" not in prompt
    assert "用户#" in prompt
    assert "199.5" in prompt


@pytest.mark.parametrize(("sql", "value"), [
    ("SELECT user_name AS label FROM orders", "假客户"),
    ("SELECT o.order_id AS label FROM orders AS o", "ORDER-SENSITIVE"),
    ("WITH names AS (SELECT user_name AS n FROM orders) SELECT LEFT(n, 2) AS label FROM names", "假客"),
    ("SELECT ASCII(user_name) AS label FROM orders", 203),
    ("SELECT (SELECT user_name FROM orders LIMIT 1) AS label", "假客户"),
    ("SELECT COUNT(*) AS label FROM orders UNION ALL SELECT ASCII(user_name) FROM orders", 203),
    ("SELECT COUNT(*) + MAX(ASCII(user_name)) AS label FROM orders", 203),
    ("SELECT future_secret_column AS label FROM orders", "未来新增的敏感值"),
])
def test_answer_prompt_masks_sensitive_projections_by_source(sql: str, value: str | int) -> None:
    rows = [{"label": value}]
    with masking_scope(_KEY) as masker:
        prompt = _adapter()._answer_prompt("按客户分组", "data", [], [], sql, rows)

    block = json.loads(prompt.split("结果：", 1)[1].split("\n", 1)[0])
    masked = block[0]["label"]
    assert masked != value and "#" in masked
    assert masker.restore(masked) == str(value)
    assert rows == [{"label": value}]


def test_result_masking_preserves_aggregates_and_business_fields() -> None:
    masker = PromptMasker(_KEY)
    sql = "SELECT COUNT(DISTINCT user_name), platform_type AS label FROM orders GROUP BY platform_type"
    rows = [{"COUNT(DISTINCT user_name)": 9, "label": "APP"}]
    assert masker.mask_rows(rows, sql) == rows


def test_average_order_value_does_not_expose_an_order_identifier() -> None:
    sql = "SELECT ROUND(SUM(payment_amount) / NULLIF(COUNT(DISTINCT order_id), 0), 2) AS aov FROM orders"
    rows = [{"aov": 143.33}]
    assert PromptMasker(_KEY).mask_rows(rows, sql) == rows


async def test_refund_planner_prompt_sends_only_pseudonyms() -> None:
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

    adapter = _adapter()
    adapter._client = Client  # type: ignore[method-assign]
    evidence = [{"tool": "overall", "rows": [{"买家": "王五", "订单数": 2}]}]
    await adapter.choose_refund_analysis("退款率为什么上升", evidence)

    assert "王五" not in captured["prompt"]
    assert "用户#" in captured["prompt"]


def test_masker_restores_only_tokens_it_issued() -> None:
    """出域假名要在返回用户前换回原值；改写到一半的假名绝不猜。"""
    masker = PromptMasker(_KEY)
    masked = masker.mask(_ROWS)[0]

    assert masker.restore(f"{masked['用户名']} 本月消费最高") == "张三 本月消费最高"
    assert masker.restore("用户#deadbeef 不是本轮客户") == "用户#deadbeef 不是本轮客户"


def test_request_scope_masks_prompt_and_restores_answer() -> None:
    """模拟 ai_service 的请求域：提示词里没有真名，最终答案里没有假名。"""
    adapter = _adapter()
    with masking_scope(_KEY) as masker:
        prompt = adapter._answer_prompt("谁的订单最多", "query", [], [], "SELECT 1", _ROWS)
    token = json.loads(prompt.split("结果：", 1)[1].split("\n", 1)[0])[0]["用户名"]

    assert "张三" not in prompt
    assert masker.restore(f"第一名是 {token}") == "第一名是 张三"


def test_state_to_response_restores_answer_but_keeps_raw_rows() -> None:
    """边界职责：给用户的正文还原假名，数据表格始终是数据库原值。"""
    masker = PromptMasker(_KEY)
    masked = masker.mask(_ROWS)[0]
    result = AgentResult(answer=f"{masked['用户名']} 消费最高", sql="SELECT 1", rows=_ROWS)
    state = {"result": result, "request_id": "req", "thread_id": "thread"}

    response = _state_to_response(state, masker)

    assert response.answer == "张三 消费最高"
    assert response.result[0]["用户名"] == "张三"


def test_prompt_masking_secret_rejects_public_or_weak_values() -> None:
    def _settings(**overrides) -> Settings:
        values = dict(_env_file=None, debug=True, admin_password="admin123")
        values.update(overrides)
        return Settings(**values)

    # DEBUG 兜底值随源码公开：不能作为假名密钥的种子
    assert _settings(jwt_secret="").prompt_masking_secret == ""
    assert _settings(jwt_secret=_DEV_JWT_PLACEHOLDER).prompt_masking_secret == ""
    assert _settings(jwt_secret="weak").prompt_masking_secret == ""
    assert _settings(jwt_secret="s" * 40).prompt_masking_secret == "s" * 40
