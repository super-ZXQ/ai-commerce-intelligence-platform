"""长期记忆规则提取、提示词格式化与 Runtime 接入测试。"""

from __future__ import annotations

import pytest

from agent_core.memory import (
    EpisodeSummary,
    EpisodeTurn,
    MemoryFact,
    MemoryTurn,
    build_episode_summary,
    extract_memories,
    format_memory_context,
    needs_episode_recall,
)
from agent_core.models import ModelResponse
from agent_core.runtime import AgentRuntime


def test_extracts_only_explicit_whitelisted_preferences() -> None:
    facts = extract_memories("记住，我默认看订单量")
    assert facts == [MemoryFact(key="default_metric", value="订单量", confidence=0.95)]

    facts = extract_memories("以后默认按平台看销售额")
    assert {(item.key, item.value) for item in facts} == {
        ("default_dimension", "平台"),
        ("default_metric", "付款金额"),
    }
    assert all(item.confidence == 0.80 for item in facts)


def test_does_not_store_one_off_filters_or_ambiguous_values() -> None:
    assert extract_memories("查一下 12 月 APP 销售额") == []
    assert extract_memories("记住，我默认看 APP 和 Web网站") == []
    assert extract_memories("记住，默认按平台和商品拆") == []
    assert extract_memories("以后其他问题别问了") == []
    assert extract_memories("默认看订单量吗") == []
    assert extract_memories("默认平台是什么") == []


def test_extracts_role_and_platform_aliases() -> None:
    assert extract_memories("请记住我是分析师") == [
        MemoryFact(key="role", value="分析师", confidence=0.95)
    ]
    assert extract_memories("记住我常用 app") == [
        MemoryFact(key="default_platform", value="APP", confidence=0.95)
    ]


def test_memory_context_is_labeled_as_reference_and_capped() -> None:
    facts = [
        MemoryFact(key="default_platform", value="APP", confidence=0.95),
        MemoryFact(key="default_metric", value="订单量", confidence=0.80),
    ]
    text = format_memory_context(facts)
    assert "仅作参考" in text
    assert "默认平台：APP" in text
    assert "默认指标：订单量" in text

    clipped = format_memory_context(facts, max_chars=20)
    assert len(clipped) == 20
    assert clipped.endswith("…")


def _runtime_with_memory(recall, writer, captured: dict, episode_recall=None, episode_writer=None) -> AgentRuntime:
    async def retrieve(_query: str):
        return []

    async def schema() -> str:
        return "orders(order_id VARCHAR, payment_amount DECIMAL)"

    async def generate(query, schema_text, history, sources, previous_error):
        captured["sql_history"] = history
        return ModelResponse("SELECT COUNT(DISTINCT order_id) AS orders FROM orders")

    async def execute(_sql: str):
        return [{"orders": 3}]

    async def answer(query, intent, history, sources, sql, rows):
        captured["answer_history"] = history
        return ModelResponse("共 3 单")

    return AgentRuntime(
        retriever=retrieve,
        schema_loader=schema,
        sql_generator=generate,
        sql_executor=execute,
        answer_generator=answer,
        memory_recall=recall,
        memory_writer=writer,
        episode_recall=episode_recall,
        episode_writer=episode_writer,
        memory_max_chars=600,
    )


@pytest.mark.asyncio
async def test_runtime_injects_memory_into_sql_and_answer_prompts() -> None:
    captured: dict = {}
    memories = [MemoryFact(key="default_metric", value="订单量", confidence=0.95)]

    async def recall(_owner: str):
        return memories

    async def writer(_turn: MemoryTurn) -> int:
        return 0

    runtime = _runtime_with_memory(recall, writer, captured)
    state = await runtime.invoke("最近订单量", owner="alice", thread_id="thread-1")

    assert state["result"].answer == "共 3 单"
    for key in ("sql_history", "answer_history"):
        assert captured[key][0]["role"] == "system"
        assert "默认指标：订单量" in captured[key][0]["content"]
    assert any(
        step.name == "load_memory" and "1 条长期偏好" in step.summary
        for step in state["result"].steps
    )


@pytest.mark.asyncio
async def test_memory_write_failure_does_not_change_the_answer() -> None:
    captured: dict = {}

    async def recall(_owner: str):
        return []

    async def writer(_turn: MemoryTurn) -> int:
        raise RuntimeError("memory database down")

    runtime = _runtime_with_memory(recall, writer, captured)
    state = await runtime.invoke("查最近销售额", owner="alice", thread_id="thread-1")

    assert state["result"].answer == "共 3 单"
    assert any(
        step.name == "save_memory" and step.status == "error"
        for step in state["result"].steps
    )


@pytest.mark.asyncio
async def test_memory_writer_receives_authenticated_owner_and_turn() -> None:
    captured: dict = {}
    turns: list[MemoryTurn] = []

    async def recall(_owner: str):
        return []

    async def writer(turn: MemoryTurn) -> int:
        turns.append(turn)
        return 1

    runtime = _runtime_with_memory(recall, writer, captured)
    state = await runtime.invoke("记住，我默认看订单量", owner="alice", thread_id="thread-1")

    assert turns[0].owner == "alice"
    assert turns[0].thread_id == "thread-1"
    assert turns[0].question == "记住，我默认看订单量"
    assert any(
        step.name == "save_memory" and "1 条长期偏好" in step.summary
        for step in state["result"].steps
    )


def test_episode_summary_is_compressed_and_skips_failed_answers() -> None:
    turn = EpisodeTurn(
        owner="alice",
        thread_id="t",
        request_id="r",
        question="12 月退款率是多少",
        answer="退款率约 13.6%",
        intent="data",
        sql="SELECT SUM(is_refund = '是') FROM orders",
    )
    summary = build_episode_summary(turn)
    assert "问题：12 月退款率是多少" in summary
    assert "结论：退款率约 13.6%" in summary
    assert "SQL：SELECT" in summary
    assert len(summary) <= 800

    assert build_episode_summary(
        EpisodeTurn("alice", "t", "r", "查退款率", "⚠️ 查询失败", "data")
    ) == ""


def test_episode_recall_only_triggers_on_history_cues() -> None:
    assert needs_episode_recall("上次那个退款分析") is True
    assert needs_episode_recall("之前 12 月的销售怎么样") is True
    assert needs_episode_recall("查一下 12 月退款率") is False

    episodes = [EpisodeSummary(7, "问题：退款分析\n结论：13.6%", 0.82, "2026-10-01")]
    text = format_memory_context([], episodes=episodes)
    assert "相关历史分析" in text
    assert "2026-10-01" in text
    assert "13.6%" in text


@pytest.mark.asyncio
async def test_runtime_injects_episode_memory_into_prompt() -> None:
    captured: dict = {}

    async def recall(_owner: str):
        return []

    async def writer(_turn: MemoryTurn) -> int:
        return 0

    async def episode_recall(_owner: str, query: str):
        assert query == "上次那个退款分析"
        return [EpisodeSummary(7, "问题：退款分析\n结论：退款率 13.6%", 0.82, "2026-10-01")]

    async def episode_writer(_turn: EpisodeTurn) -> bool:
        return False

    runtime = _runtime_with_memory(
        recall,
        writer,
        captured,
        episode_recall=episode_recall,
        episode_writer=episode_writer,
    )
    state = await runtime.invoke("上次那个退款分析", owner="alice", thread_id="thread-1")

    history = captured["answer_history"]
    assert history[0]["role"] == "system"
    assert "相关历史分析" in history[0]["content"]
    assert any(
        step.name == "load_memory" and "1 条历史情节" in step.summary
        for step in state["result"].steps
    )


@pytest.mark.asyncio
async def test_episode_write_failure_does_not_change_the_answer() -> None:
    captured: dict = {}

    async def recall(_owner: str):
        return []

    async def writer(_turn: MemoryTurn) -> int:
        return 0

    async def episode_writer(_turn: EpisodeTurn) -> bool:
        raise RuntimeError("episode database down")

    runtime = _runtime_with_memory(
        recall,
        writer,
        captured,
        episode_writer=episode_writer,
    )
    state = await runtime.invoke("查最近销售额", owner="alice", thread_id="thread-1")

    assert state["result"].answer == "共 3 单"
    assert any(
        step.name == "save_episode" and step.status == "error"
        for step in state["result"].steps
    )
