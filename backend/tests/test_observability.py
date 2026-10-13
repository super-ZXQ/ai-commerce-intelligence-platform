"""Langfuse trace 观测测试：开关语义、trace_id 派生、汇总上报、Runtime 接线。"""

from __future__ import annotations

import sys
import types
import uuid

import pytest

from agent_core import observability as obs
from agent_core.models import AgentResult, AgentSource, AgentStep, AgentUsage
from agent_core.runtime import AgentRuntime


@pytest.fixture(autouse=True)
def _fresh_gateway(monkeypatch):
    """每个测试拿到全新的门面单例，避免 _resolved 缓存串味。"""
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_PRICE_INPUT_PER_MTOK", raising=False)
    monkeypatch.delenv("LANGFUSE_PRICE_OUTPUT_PER_MTOK", raising=False)
    monkeypatch.delenv("LANGFUSE_FLUSH_EACH_QUERY", raising=False)
    monkeypatch.setattr(obs, "_gateway", obs._LangfuseGateway())
    yield


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.events: list[dict] = []
        self.flush_calls = 0
        _FakeClient.instances.append(self)

    def create_event(self, **kwargs):
        self.events.append(kwargs)

    def flush(self):
        self.flush_calls += 1


class _FakeHandler:
    created: list[dict] = []

    def __init__(self, **kwargs):
        _FakeHandler.created.append(kwargs)


@pytest.fixture
def fake_langfuse(monkeypatch):
    """注入假的 langfuse / langfuse.langchain 模块（无需真实服务）。"""
    _FakeClient.instances.clear()
    _FakeHandler.created.clear()

    root = types.ModuleType("langfuse")
    root.Langfuse = _FakeClient
    sub = types.ModuleType("langfuse.langchain")
    sub.CallbackHandler = _FakeHandler
    monkeypatch.setitem(sys.modules, "langfuse", root)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", sub)
    return root


# ─────────────────────── 开关语义：默认关闭 ───────────────────────

def test_disabled_by_default_builds_no_config():
    assert obs.build_query_config(request_id=str(uuid.uuid4()), owner="u", query="q", thread_id="t") is None


def test_disabled_summary_is_noop():
    obs.record_query_summary(
        str(uuid.uuid4()), intent="data", usage=None, latency_ms=1, step_names=[], sql_error=None
    )  # 不得抛异常


def test_enabled_without_keys_stays_disabled(monkeypatch, fake_langfuse):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    assert obs.build_query_config(request_id=str(uuid.uuid4()), owner="u", query="q", thread_id="t") is None


def test_enabled_without_sdk_stays_disabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    monkeypatch.setitem(sys.modules, "langfuse", None)  # 模拟 ImportError
    monkeypatch.setitem(sys.modules, "langfuse.langchain", None)
    assert obs.build_query_config(request_id=str(uuid.uuid4()), owner="u", query="q", thread_id="t") is None


# ─────────────────────── trace_id 派生 ───────────────────────

def test_trace_id_is_uuid_hex():
    request_id = str(uuid.uuid4())
    assert obs.trace_id_for(request_id) == request_id.replace("-", "")
    assert len(obs.trace_id_for(request_id)) == 32


def test_trace_id_falls_back_for_non_uuid():
    trace_id = obs.trace_id_for("not-a-uuid")
    assert len(trace_id) == 32
    int(trace_id, 16)  # 必须是合法 hex（Langfuse v3 要求）
    assert trace_id == obs.trace_id_for("not-a-uuid")  # 确定性派生


# ─────────────────────── 启用后的 config 与汇总 ───────────────────────

def test_build_query_config_attaches_handler_and_metadata(monkeypatch, fake_langfuse):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    request_id = str(uuid.uuid4())

    config = obs.build_query_config(request_id=request_id, owner="alice", query="最近销售额", thread_id="t-1")

    assert config is not None
    assert len(config["callbacks"]) == 1
    assert config["metadata"]["langfuse_trace_id"] == request_id.replace("-", "")
    assert config["metadata"]["owner"] == "alice"
    assert config["metadata"]["query_length"] == len("最近销售额")
    # handler 以 public_key + trace_context 构造（secret/host 走环境变量）
    assert _FakeHandler.created[-1]["public_key"] == "pk-lf-x"
    assert _FakeHandler.created[-1]["trace_context"]["trace_id"] == request_id.replace("-", "")


def test_record_summary_emits_event_to_same_trace(monkeypatch, fake_langfuse):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    monkeypatch.setenv("LANGFUSE_PRICE_INPUT_PER_MTOK", "0.6")
    monkeypatch.setenv("LANGFUSE_PRICE_OUTPUT_PER_MTOK", "0.6")
    request_id = str(uuid.uuid4())

    # 先走一遍 build 让 client 初始化
    obs.build_query_config(request_id=request_id, owner="alice", query="q", thread_id="t")
    obs.record_query_summary(
        request_id,
        intent="data",
        usage=AgentUsage(latency_ms=3200, input_tokens=800, output_tokens=200, total_tokens=1000),
        latency_ms=3200,
        step_names=["route", "generate_sql", "execute_sql", "synthesize"],
        sql_error=None,
    )

    client = _FakeClient.instances[-1]
    assert len(client.events) == 1
    event = client.events[0]
    assert event["name"] == "agent-finalize"
    assert event["trace_context"]["trace_id"] == request_id.replace("-", "")
    meta = event["metadata"]
    assert meta["intent"] == "data"
    assert meta["total_tokens"] == 1000
    assert meta["latency_ms"] == 3200
    assert meta["steps"] == ["route", "generate_sql", "execute_sql", "synthesize"]
    # 800/1M*0.6 + 200/1M*0.6 = 0.0006
    assert meta["estimated_cost_usd"] == pytest.approx(0.0006)


def test_record_summary_without_flush_by_default(monkeypatch, fake_langfuse):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    obs.record_query_summary(str(uuid.uuid4()), intent="data", usage=None, latency_ms=1, step_names=[], sql_error=None)
    assert _FakeClient.instances[-1].flush_calls == 0


def test_record_summary_flushes_when_configured(monkeypatch, fake_langfuse):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    monkeypatch.setenv("LANGFUSE_FLUSH_EACH_QUERY", "true")
    obs.record_query_summary(str(uuid.uuid4()), intent="data", usage=None, latency_ms=1, step_names=[], sql_error=None)
    assert _FakeClient.instances[-1].flush_calls == 1


def test_record_summary_swallows_client_errors(monkeypatch, fake_langfuse):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")

    class _BrokenClient(_FakeClient):
        def create_event(self, **kwargs):
            raise RuntimeError("langfuse down")

    monkeypatch.setattr(sys.modules["langfuse"], "Langfuse", _BrokenClient)
    # 观测失败不得影响调用方
    obs.record_query_summary(str(uuid.uuid4()), intent="data", usage=None, latency_ms=1, step_names=[], sql_error=None)


# ─────────────────────── 成本估算 ───────────────────────

def test_cost_none_without_prices(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PRICE_INPUT_PER_MTOK", raising=False)
    monkeypatch.delenv("LANGFUSE_PRICE_OUTPUT_PER_MTOK", raising=False)
    assert obs.estimate_cost_usd(1000, 500) is None


def test_cost_partial_prices(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PRICE_OUTPUT_PER_MTOK", "2.0")
    monkeypatch.delenv("LANGFUSE_PRICE_INPUT_PER_MTOK", raising=False)
    assert obs.estimate_cost_usd(1000, 500_000) == pytest.approx(1.0)


# ─────────────────────── Runtime 接线 ───────────────────────

def _runtime() -> AgentRuntime:
    async def retrieve(_query: str) -> list[AgentSource]:
        return []

    async def schema() -> str:
        return "orders(payment_amount DECIMAL)"

    async def generate(_q, _s, _h, _src, _e):
        from agent_core.models import ModelResponse

        return ModelResponse("SELECT 1", total_tokens=5)

    async def execute(_sql: str) -> list[dict]:
        return [{"v": 1}]

    async def answer(_q, _intent, _h, _src, _sql, _rows):
        from agent_core.models import ModelResponse

        return ModelResponse("结果是 1", total_tokens=5)

    return AgentRuntime(
        retriever=retrieve, schema_loader=schema, sql_generator=generate,
        sql_executor=execute, answer_generator=answer,
    )


@pytest.mark.asyncio
async def test_invoke_reports_trace_summary_when_enabled(monkeypatch):
    configs: list[dict] = []
    summaries: list[dict] = []

    def fake_build(**kwargs):
        configs.append(kwargs)
        return {"callbacks": []}

    def fake_summary(request_id, **kwargs):
        summaries.append({"request_id": request_id, **kwargs})

    monkeypatch.setattr(obs, "build_query_config", fake_build)
    monkeypatch.setattr(obs, "record_query_summary", fake_summary)

    state = await _runtime().invoke("最近30天销售额", owner="alice", thread_id="t-1")

    assert state["result"].answer
    assert len(configs) == 1
    assert configs[0]["request_id"] == state["request_id"]
    assert configs[0]["thread_id"] == "t-1"
    assert len(summaries) == 1
    assert summaries[0]["request_id"] == state["request_id"]
    assert summaries[0]["intent"] == "data"
    assert "generate_sql" in summaries[0]["step_names"]


@pytest.mark.asyncio
async def test_astream_reports_trace_summary_when_enabled(monkeypatch):
    summaries: list[dict] = []
    monkeypatch.setattr(obs, "build_query_config", lambda **kwargs: {"callbacks": []})
    monkeypatch.setattr(obs, "record_query_summary", lambda request_id, **kw: summaries.append({"request_id": request_id, **kw}))

    steps: list[dict] = []
    state = await _runtime().astream("最近30天销售额", owner="alice", on_step=steps.append)

    assert state["result"].answer
    assert len(steps) >= 4
    assert len(summaries) == 1
    assert summaries[0]["request_id"] == state["request_id"]


@pytest.mark.asyncio
async def test_disabled_observability_leaves_query_untouched():
    """未配置 Langfuse 时查询链路与启用前完全一致（回归保护）。"""
    state = await _runtime().invoke("最近30天销售额", owner="alice")
    result: AgentResult = state["result"]
    assert result.intent == "data"
    assert [step.name for step in result.steps][0] == "input_safety"
    assert result.usage.total_tokens == 10
