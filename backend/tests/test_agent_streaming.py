"""Agent 流式执行（runtime.astream / astream_answer）测试。"""

from __future__ import annotations

import pytest

from agent_core.models import AgentSource, ModelResponse
from agent_core.runtime import AgentRuntime
from agent_core.streaming import emit_token, reset_token_hook, set_token_hook


class _FakeChunk:
    """模拟 ChatOpenAI 流式 chunk：content 为增量文本，末块可带 usage。"""

    def __init__(self, content: str, usage: dict | None = None):
        self.content = content
        self.usage_metadata = usage


class _FakeStreamClient:
    def __init__(self, chunks: list[_FakeChunk]):
        self._chunks = chunks
        self.calls: list[dict] = []

    def astream(self, _prompt):
        self.calls.append({"streaming": True})

        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()

    async def ainvoke(self, _prompt):
        self.calls.append({"streaming": False})
        return _FakeChunk("".join(c.content for c in self._chunks), self._chunks[-1].usage_metadata)


def _runtime(calls: list[str], *, answer_tokens: list[str] | None = None) -> AgentRuntime:
    async def retrieve(_query: str) -> list[AgentSource]:
        calls.append("retrieve")
        return [AgentSource(filename="metrics.md", section="客单价", doc_type="markdown", score=0.9, snippet="定义")]

    async def schema() -> str:
        calls.append("schema")
        return "orders(payment_amount DECIMAL)"

    async def generate(_query, _schema, _history, _sources, _error) -> ModelResponse:
        calls.append("generate")
        return ModelResponse("SELECT SUM(payment_amount) AS sales FROM orders", total_tokens=5)

    async def execute(_sql: str) -> list[dict]:
        calls.append("execute")
        return [{"sales": 10}]

    async def answer(_query, _intent, _history, _sources, _sql, _rows) -> ModelResponse:
        calls.append("answer")
        for token in answer_tokens or []:
            emit_token(token)
        return ModelResponse("完成", input_tokens=2, output_tokens=1, total_tokens=3)

    return AgentRuntime(
        retriever=retrieve,
        schema_loader=schema,
        sql_generator=generate,
        sql_executor=execute,
        answer_generator=answer,
    )


@pytest.mark.asyncio
async def test_astream_emits_steps_in_order_and_returns_invoke_compatible_state() -> None:
    calls: list[str] = []
    runtime = _runtime(calls)
    events: list[dict] = []

    state = await runtime.astream("查询最近销售额", on_step=events.append)

    # 步骤事件按节点执行顺序到达，字段与 AgentStep 一致
    # （_finalize 只产出 result，不追加步骤事件，因此到 save_session 为止）
    assert [event["name"] for event in events] == [
        "input_safety", "load_history", "route", "load_schema",
        "generate_sql", "validate_sql", "execute_sql", "synthesize",
        "save_session",
    ]
    assert all({"name", "status", "duration_ms", "summary"} <= set(event) for event in events)
    # 返回结构与 invoke() 一致：调用方后处理无需分支
    assert state["answer"] == "完成"
    assert state["result"].answer == "完成"
    assert len(state["result"].steps) == len(events)
    assert calls == ["schema", "generate", "execute", "answer"]


@pytest.mark.asyncio
async def test_astream_matches_invoke_result_shape() -> None:
    invoke_state = await _runtime([]).invoke("查询最近销售额")
    astream_state = await _runtime([]).astream("查询最近销售额")

    assert astream_state["answer"] == invoke_state["answer"]
    assert astream_state["sql"] == invoke_state["sql"]
    assert astream_state["rows"] == invoke_state["rows"]
    assert [s.name for s in astream_state["result"].steps] == [s.name for s in invoke_state["result"].steps]
    assert astream_state["result"].usage.total_tokens == invoke_state["result"].usage.total_tokens


@pytest.mark.asyncio
async def test_astream_without_callbacks_behaves_like_invoke() -> None:
    state = await _runtime([]).astream("查询最近销售额")

    assert state["answer"] == "完成"
    assert state["result"] is not None


@pytest.mark.asyncio
async def test_astream_forwards_tokens_from_answer_generator() -> None:
    tokens: list[str] = []
    runtime = _runtime([], answer_tokens=["部", "分"])

    state = await runtime.astream("查询最近销售额", on_token=tokens.append)

    assert tokens == ["部", "分"]
    assert state["answer"] == "完成"


@pytest.mark.asyncio
async def test_astream_resets_token_hook_after_completion() -> None:
    runtime = _runtime([], answer_tokens=["A"])
    leaked: list[str] = []

    await runtime.astream("查询最近销售额", on_token=lambda _d: None)
    # astream 结束后钩子必须复位：后续 emit 不应再打到旧回调
    emit_token("leak")
    assert leaked == []


def test_emit_token_without_hook_is_noop() -> None:
    emit_token("静默丢弃")


@pytest.mark.asyncio
async def test_astream_answer_streams_tokens_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_core.model_adapter import OpenAIModelAdapter

    adapter = OpenAIModelAdapter(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="test-model",
        business_context="ctx",
    )
    client = _FakeStreamClient([
        _FakeChunk("共 "),
        _FakeChunk("3 行。", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
    ])
    monkeypatch.setattr(adapter, "_client", lambda **_kwargs: client)

    tokens: list[str] = []
    hook = set_token_hook(tokens.append)
    try:
        response = await adapter.astream_answer("总销售额", "data", [], [], "SELECT 1", [{"sales": 1}])
    finally:
        reset_token_hook(hook)

    assert tokens == ["共 ", "3 行。"]
    assert response.content == "共 3 行。"
    assert response.total_tokens == 15
    # 流式路径必须真正走 astream（带 stream_usage 标记的客户端）
    assert client.calls == [{"streaming": True}]


@pytest.mark.asyncio
async def test_astream_answer_without_api_key_delegates_to_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_core.model_adapter import OpenAIModelAdapter

    adapter = OpenAIModelAdapter(
        api_key="",
        base_url="https://api.deepseek.com",
        model="m",
        business_context="ctx",
    )
    client = _FakeStreamClient([_FakeChunk("不应被调用")])
    monkeypatch.setattr(adapter, "_client", lambda **_kwargs: client)

    sources = [AgentSource(filename="metrics.md", section="客单价", snippet="定义")]
    response = await adapter.astream_answer("客单价定义", "knowledge", [], sources, None, [])

    assert "最相关的业务知识" in response.content
    assert client.calls == []
