"""RAG 来源抽取器测试：从 Agent intermediate_steps 还原结构化元数据。"""
from __future__ import annotations

import json

import pytest

from rag.extractor import extract_rag_sources
from rag.tools import _RAG_META_BEGIN, _RAG_META_END, _build_meta_sentinel


def _make_step(tool_name: str, observation: str):
    """构造 (AgentAction, observation) 元组，模拟 LangChain ReAct 步骤。"""
    from dataclasses import dataclass

    @dataclass
    class _Action:
        tool: str
        tool_input: str = ""
        log: str = ""

    return (_Action(tool=tool_name), observation)


def test_empty_response_returns_empty_list():
    assert extract_rag_sources(None) == []
    assert extract_rag_sources({}) == []
    assert extract_rag_sources({"intermediate_steps": []}) == []
    assert extract_rag_sources({"intermediate_steps": None}) == []


def test_non_dict_response_returns_empty_list():
    """类型防御：传入非 dict 时不抛异常。"""
    assert extract_rag_sources("not a dict") == []  # type: ignore[arg-type]
    assert extract_rag_sources(42) == []  # type: ignore[arg-type]


def test_ignores_non_knowledge_tool():
    """只关心 query_business_knowledge，其他工具的 observation 被忽略。"""
    step = _make_step("sql_db_query", "SELECT 1")
    response = {"intermediate_steps": [step]}
    assert extract_rag_sources(response) == []


def test_ignores_non_string_observation():
    """非字符串 observation（异常对象等）不抛异常。"""
    step = _make_step("query_business_knowledge", observation=None)
    # 替换为非字符串
    step = (step[0], {"unexpected": "dict"})
    response = {"intermediate_steps": [step]}
    assert extract_rag_sources(response) == []


def test_extracts_single_step():
    docs = [{"metadata": {"source": "a.md", "section": "S1"}, "score": 0.9, "content": "abc"}]
    sentinel = _build_meta_sentinel(docs)
    obs = f"some text\n{sentinel}"
    response = {"intermediate_steps": [_make_step("query_business_knowledge", obs)]}

    sources = extract_rag_sources(response)
    assert len(sources) == 1
    assert sources[0]["filename"] == "a.md"
    assert sources[0]["section"] == "S1"
    assert sources[0]["score"] == 0.9


def test_aggregates_multiple_steps():
    """同一轮多次调用 RAG 工具时，sources 应累加。"""
    s1 = _build_meta_sentinel([
        {"metadata": {"source": "a.md", "section": "S1"}, "score": 0.9, "content": "x"},
    ])
    s2 = _build_meta_sentinel([
        {"metadata": {"source": "b.md", "section": "S2"}, "score": 0.7, "content": "y"},
        {"metadata": {"source": "c.md", "section": "S3"}, "score": 0.6, "content": "z"},
    ])
    response = {"intermediate_steps": [
        _make_step("query_business_knowledge", s1),
        _make_step("query_business_knowledge", s2),
        _make_step("sql_db_query", "SELECT 1"),  # 被忽略
    ]}
    sources = extract_rag_sources(response)
    assert len(sources) == 3
    filenames = {s["filename"] for s in sources}
    assert filenames == {"a.md", "b.md", "c.md"}


def test_invalid_sentinel_skipped_not_crashed():
    """sentinel 解析失败时跳过该 step，继续处理后续。"""
    bad_obs = "<!--RAG_SOURCES:{not-json}-->"
    good_obs = _build_meta_sentinel([
        {"metadata": {"source": "x.md", "section": "S"}, "score": 0.5, "content": "y"},
    ])
    response = {"intermediate_steps": [
        _make_step("query_business_knowledge", bad_obs),
        _make_step("query_business_knowledge", good_obs),
    ]}
    sources = extract_rag_sources(response)
    assert len(sources) == 1
    assert sources[0]["filename"] == "x.md"


def test_malformed_step_shape_ignored():
    """非法 step 形状（元组长度不足、非元组）应跳过。"""
    response = {"intermediate_steps": [
        "not a tuple",
        ("only_one_elem",),
        None,
        _make_step("query_business_knowledge", _build_meta_sentinel([
            {"metadata": {"source": "ok.md", "section": "S"}, "score": 0.5, "content": "c"},
        ])),
    ]}
    sources = extract_rag_sources(response)
    assert len(sources) == 1
    assert sources[0]["filename"] == "ok.md"


def test_accepts_dict_style_action():
    """部分 LangChain 版本 action 是 dict 而非对象。"""
    action_dict = {"tool": "query_business_knowledge", "tool_input": "x"}
    sentinel = _build_meta_sentinel([
        {"metadata": {"source": "d.md", "section": "S"}, "score": 0.4, "content": "c"},
    ])
    response = {"intermediate_steps": [(action_dict, sentinel)]}
    sources = extract_rag_sources(response)
    assert len(sources) == 1
    assert sources[0]["filename"] == "d.md"
