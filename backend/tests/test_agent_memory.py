"""长期记忆规则提取、提示词格式化与 Runtime 接入测试。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

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


def _runtime_with_memory(
    recall, writer, captured: dict, episode_recall=None, episode_writer=None, memory_max_chars=600
) -> AgentRuntime:
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
        memory_max_chars=memory_max_chars,
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
        # 记忆是不可信数据的回流，绝不能占用 system 指令通道。
        assert captured[key][0]["role"] == "reference"
        assert "不是指令" in captured[key][0]["content"]
        assert "默认指标：订单量" in captured[key][0]["content"]
    assert any(
        step.name == "load_memory" and "1 条长期偏好" in step.summary
        for step in state["result"].steps
    )


@pytest.mark.asyncio
async def test_memory_fence_stays_inside_the_char_budget() -> None:
    """围栏与记忆共用一个预算：合成消息一旦超出 max_chars 会被历史窗口截尾。"""
    captured: dict = {}

    async def recall(_owner: str):
        return [MemoryFact(key="default_metric", value="订单量", confidence=0.95)]

    async def writer(_turn: MemoryTurn) -> int:
        return 0

    runtime = _runtime_with_memory(recall, writer, captured, memory_max_chars=100)
    await runtime.invoke("最近订单量", owner="alice", thread_id="thread-1")

    message = captured["sql_history"][0]
    assert message["role"] == "reference"
    assert len(message["content"]) <= 100


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
    assert history[0]["role"] == "reference"
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


async def _assert_expiry_on_app_clock(owner: str, model, ttl_days: int) -> None:
    """expires_at 必须落在应用的 UTC-naive 时钟基上。

    MySQL 的 NOW() 受 @@session.time_zone 影响（当前部署返回 +08），所以有效期不能
    和服务端时钟对账；这条断言锁住「写入端与过滤端同一时钟」这个不变量。
    """
    from sqlalchemy import delete, select

    from backend.database import auth_session_factory

    try:
        async with auth_session_factory() as session:
            row = (
                await session.execute(select(model).where(model.owner == owner))
            ).scalar_one()
        assert row.expires_at.tzinfo is None, "DATETIME 列不允许写带时区的值"
        expected = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=ttl_days)
        skew = abs((row.expires_at - expected).total_seconds())
        assert skew < 120, f"expires_at 偏离应用 UTC 时钟基 {skew / 3600:.2f} 小时"
    finally:
        async with auth_session_factory() as session:
            await session.execute(delete(model).where(model.owner == owner))
            await session.commit()


@pytest.mark.asyncio
async def test_preference_expiry_is_written_on_the_app_clock() -> None:
    from backend.config import get_settings
    from backend.models.database_models import AgentMemoryFact
    from backend.services import memory_service

    owner = f"pytest-ttl-{uuid.uuid4().hex[:10]}"
    written = await memory_service.remember_turn(
        MemoryTurn(
            owner=owner,
            thread_id="thread-ttl",
            request_id=f"req-{uuid.uuid4().hex[:8]}-ttl",
            question="记住，我默认看订单量",
        )
    )
    assert written == 1

    await _assert_expiry_on_app_clock(owner, AgentMemoryFact, get_settings().agent_memory_ttl_days)


@pytest.mark.asyncio
async def test_expired_preference_is_not_recalled() -> None:
    """过期判定与写入同一时钟：过去的时间点必须召回为空。"""
    from sqlalchemy import delete, select

    from backend.database import auth_session_factory
    from backend.models.database_models import AgentMemoryFact
    from backend.services import memory_service

    owner = f"pytest-ttl-{uuid.uuid4().hex[:10]}"
    request_id = f"req-{uuid.uuid4().hex[:8]}-ttl"
    try:
        assert await memory_service.remember_turn(
            MemoryTurn(
                owner=owner,
                thread_id="thread-ttl",
                request_id=request_id,
                question="记住，我默认看订单量",
            )
        ) == 1
        assert await memory_service.recall_memories(owner)

        async with auth_session_factory() as session:
            row = (
                await session.execute(
                    select(AgentMemoryFact).where(AgentMemoryFact.owner == owner)
                )
            ).scalar_one()
            row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
            await session.commit()

        assert await memory_service.recall_memories(owner) == []
    finally:
        async with auth_session_factory() as session:
            await session.execute(delete(AgentMemoryFact).where(AgentMemoryFact.owner == owner))
            await session.commit()


@pytest.mark.asyncio
async def test_episode_expiry_is_written_on_the_app_clock(monkeypatch) -> None:
    from backend.models.database_models import AgentMemoryEpisode
    from backend.services import episode_memory_service

    monkeypatch.setattr(episode_memory_service, "_schedule_embedding", lambda *args: None)
    ttl_days = episode_memory_service.settings.agent_episode_ttl_days
    owner = f"pytest-ttl-{uuid.uuid4().hex[:10]}"
    saved = await episode_memory_service.save_episode(
        EpisodeTurn(
            owner=owner,
            thread_id="thread-ttl",
            request_id=f"req-{uuid.uuid4().hex[:8]}-ttl",
            question="上月的退款率是多少",
            answer="退款率 13.6%",
            intent="analysis",
        )
    )
    assert saved is True

    await _assert_expiry_on_app_clock(owner, AgentMemoryEpisode, ttl_days)
