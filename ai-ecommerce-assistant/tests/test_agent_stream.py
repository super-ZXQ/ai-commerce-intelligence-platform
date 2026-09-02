"""agent_stream 流式执行测试。

覆盖三类场景：
1. FinalAnswerDetector 前缀探测状态机（含前缀跨 token 拆分）
2. 真实 AgentExecutor 集成：SQLite + FakeListChatModel 驱动
   create_sql_agent，验证 astream_events 重组出的 response dict
   与 invoke() 结构一致、步骤/token 回调正确触发
3. 根链路事件缺失时的兜底重建

全程零 API 成本：LLM 用 FakeListChatModel 按脚本回放。
"""
from __future__ import annotations

import warnings

import pytest
from sqlalchemy import create_engine, text
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_community.agent_toolkits import SQLDatabaseToolkit, create_sql_agent
from langchain_community.utilities.sql_database import SQLDatabase

from agent_stream import (
    FINAL_PREFIX,
    FinalAnswerDetector,
    stream_agent_response,
)

warnings.filterwarnings("ignore", message=".*langchain-community.*")


# ---------------------------------------------------------------------------
# FinalAnswerDetector 单元测试
# ---------------------------------------------------------------------------

def test_detector_matches_when_prefix_split_across_tokens():
    """前缀被拆成多个 token（如逐字符流）时仍能命中。"""
    det = FinalAnswerDetector()
    deltas = [det.feed(t) for t in ("Thought: 前一轮\nAction: x\n",)]  # 先来一轮思考步
    assert deltas == [None]

    det2 = FinalAnswerDetector()
    got = []
    for ch in "Final Answer: 共 3 行。":
        got.append(det2.feed(ch))
    # 命中前缀后下发增量（跳过紧邻空白），思考轮 token 为 None
    assert det2.state == "matched"
    assert det2.answer == "共 3 行。"
    assert "".join(d for d in got if d) == "共 3 行。"


def test_detector_rejects_thought_output():
    """思考步输出偏离前缀 → 整轮 rejected，后续 token 全部忽略。"""
    det = FinalAnswerDetector()
    assert det.feed("Thought: 我需要查看表结构\n") is None
    assert det.feed("Action: sql_db_schema\n") is None
    assert det.state == "rejected"
    assert det.feed("Final Answer: 不该出现") is None
    assert det.answer == ""


def test_detector_leading_whitespace_prefix():
    """前缀前有换行/空白（ReAct 输出常见）时仍能命中。"""
    det = FinalAnswerDetector()
    det.feed("\n\n")
    delta = det.feed("Final Answer:")
    assert det.state == "matched"
    assert delta is None  # 前缀刚结束，尚无正文
    assert det.feed(" 42%") == "42%"
    assert det.answer == "42%"


def test_detector_pending_never_resolved():
    """输出短于前缀且流提前结束 → 停在 pending，不误判。"""
    det = FinalAnswerDetector()
    det.feed("Final Answ")
    assert det.state == "pending"
    assert det.answer == ""


def test_detector_candidate_prefix_waits():
    """只到 "Final" 时仍是候选，不能判负。"""
    det = FinalAnswerDetector()
    assert det.feed("Final") is None
    assert det.state == "pending"
    assert det.feed(" Answer: 结果") == "结果"


# ---------------------------------------------------------------------------
# 集成测试：真实 AgentExecutor + SQLite + FakeListChatModel
# ---------------------------------------------------------------------------

@pytest.fixture
def sqlite_db(tmp_path):
    """临时文件型 SQLite，避免内存库多连接隔离问题。"""
    path = tmp_path / "orders.db"
    eng = create_engine(f"sqlite:///{path}")
    with eng.connect() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, amount REAL)"))
        c.execute(text("INSERT INTO orders VALUES (1, 10.0), (2, 20.0)"))
        c.commit()
    return SQLDatabase(engine=eng)


def _build_agent(sqlite_db):
    llm = FakeListChatModel(responses=[
        "Thought: 我需要查询订单行数\n"
        "Action: sql_db_query\n"
        "Action Input: SELECT COUNT(*) FROM orders",
        "Final Answer: 表中共有 2 行数据。",
    ])
    toolkit = SQLDatabaseToolkit(db=sqlite_db, llm=llm)
    return create_sql_agent(
        llm=llm,
        toolkit=toolkit,
        agent_type="zero-shot-react-description",
        agent_executor_kwargs={"return_intermediate_steps": True},
        verbose=False,
    )


def test_stream_agent_response_matches_invoke_shape(sqlite_db):
    """重组的 response dict 与 invoke() 返回结构一致，后处理零改动。"""
    agent = _build_agent(sqlite_db)
    steps: list[dict] = []
    tokens: list[str] = []

    response = stream_agent_response(
        agent, "订单有多少行",
        on_step=steps.append,
        on_token=tokens.append,
    )

    assert set(response.keys()) >= {"output", "intermediate_steps"}
    assert response["output"] == "表中共有 2 行数据。"
    assert len(response["intermediate_steps"]) == 1
    action, observation = response["intermediate_steps"][0]
    assert action.tool == "sql_db_query"
    assert "SELECT COUNT(*) FROM orders" in action.tool_input
    assert "2" in str(observation)

    # 步骤回调：sql_db_query 先 running 后 done
    query_steps = [s for s in steps if s["tool"] == "sql_db_query"]
    assert [s["status"] for s in query_steps] == ["running", "done"]
    assert query_steps[0]["label"] == "执行 SQL 查询"

    # token 回调拼接后等于最终答案（前缀本身不下发）
    assert "".join(tokens) == "表中共有 2 行数据。"


def test_stream_agent_response_without_callbacks(sqlite_db):
    """回调全部可省略（纯执行模式），仍返回完整 response dict。"""
    agent = _build_agent(sqlite_db)
    response = stream_agent_response(agent, "订单有多少行")
    assert response["output"] == "表中共有 2 行数据。"
    assert len(response["intermediate_steps"]) == 1


# ---------------------------------------------------------------------------
# 兜底路径：根链路事件缺失时重建 response dict
# ---------------------------------------------------------------------------

class FakeStreamingAgent:
    """只吐工具事件与 token 流、不吐根链路 on_chain_end 的假 Agent。"""

    def __init__(self, events: list[dict]):
        self._events = events

    async def astream_events(self, inp, version=None):
        for ev in self._events:
            yield ev


def _fake_events() -> list[dict]:
    """构造一次「工具调用 + Final Answer 流」的最小事件序列。"""
    events = [
        {
            "event": "on_tool_start",
            "name": "sql_db_query",
            "run_id": "tool-1",
            "data": {"input": "SELECT 1"},
        },
        {
            "event": "on_tool_end",
            "name": "sql_db_query",
            "run_id": "tool-1",
            "data": {"output": "[(1,)]"},
        },
    ]
    # Final Answer 逐 token 拆分下发
    for ch in "Final Answer: 兜底答案":
        events.append({
            "event": "on_chat_model_stream",
            "name": "FakeModel",
            "run_id": "llm-1",
            "data": {"chunk": _StrChunk(ch)},
        })
    return events


class _StrChunk:
    """最小 chunk 桩：只暴露 content 属性。"""

    def __init__(self, content: str):
        self.content = content


def test_fallback_response_rebuilt_without_root_output():
    agent = FakeStreamingAgent(_fake_events())
    tokens: list[str] = []
    response = stream_agent_response(agent, "问题", on_token=tokens.append)

    assert response["output"] == "兜底答案"
    assert len(response["intermediate_steps"]) == 1
    action, obs = response["intermediate_steps"][0]
    assert action.tool == "sql_db_query"
    assert action.tool_input == "SELECT 1"
    assert obs == "[(1,)]"
    assert "".join(tokens) == "兜底答案"


def test_final_prefix_constant():
    assert FINAL_PREFIX == "Final Answer:"
