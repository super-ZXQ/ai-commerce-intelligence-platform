"""RAG 工具层测试：sentinel 序列化 + Tool 工厂。

不依赖真实 VectorStore，全部用 Mock。
"""
from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import pytest

from rag.tools import (
    _RAG_META_BEGIN,
    _RAG_META_END,
    _build_meta_sentinel,
    build_knowledge_tool,
)


def test_sentinel_empty_list():
    """空列表应序列化为空数组 sentinel。"""
    s = _build_meta_sentinel([])
    assert s == f"{_RAG_META_BEGIN}[]{_RAG_META_END}"


def test_sentinel_serialize_full_fields():
    docs = [
        {
            "metadata": {"source": "/abs/path/biz_glossary.md", "section": "复购率"},
            "score": 0.82,
            "content": "复购率 = 消费2次及以上的用户 / 总用户",
        },
        {
            "metadata": {"source": "kpi.md", "section": "客单价"},
            "score": 0.55,
            "content": "客单价 = 总付款金额 / 总订单数",
        },
    ]
    s = _build_meta_sentinel(docs)
    assert s.startswith(_RAG_META_BEGIN) and s.endswith(_RAG_META_END)
    payload = json.loads(s[len(_RAG_META_BEGIN):-len(_RAG_META_END)])
    assert len(payload) == 2

    # 第一条
    assert payload[0]["rank"] == 1
    assert payload[0]["filename"] == "biz_glossary.md"  # 自动取 basename
    assert payload[0]["section"] == "复购率"
    assert payload[0]["score"] == 0.82
    assert payload[0]["preview"].startswith("复购率")

    # 第二条
    assert payload[1]["rank"] == 2
    assert payload[1]["filename"] == "kpi.md"
    assert payload[1]["score"] == 0.55


def test_sentinel_truncates_preview_to_200_chars():
    """preview 截断到 200 字符，避免 sentinel 膨胀。"""
    docs = [{"metadata": {}, "score": 0.5, "content": "x" * 1000}]
    s = _build_meta_sentinel(docs)
    payload = json.loads(s[len(_RAG_META_BEGIN):-len(_RAG_META_END)])
    assert len(payload[0]["preview"]) == 200


def test_sentinel_filename_default_unknown():
    """metadata 缺 source 时，filename 兜底为 'unknown'。"""
    docs = [{"metadata": {}, "score": 0.1, "content": "x"}]
    s = _build_meta_sentinel(docs)
    payload = json.loads(s[len(_RAG_META_BEGIN):-len(_RAG_META_END)])
    assert payload[0]["filename"] == "unknown"


def test_sentinel_score_rounded():
    """score 保留 4 位小数。"""
    docs = [{"metadata": {}, "score": 0.123456789, "content": "x"}]
    s = _build_meta_sentinel(docs)
    payload = json.loads(s[len(_RAG_META_BEGIN):-len(_RAG_META_END)])
    assert payload[0]["score"] == 0.1235  # round 4 位


# ──────────────────── Tool 工厂测试 ────────────────────


def _mock_retriever(search_return: list | None = None,
                    side_effect: Exception | None = None) -> MagicMock:
    r = MagicMock()
    if side_effect is not None:
        r.retrieve.side_effect = side_effect
    else:
        r.retrieve.return_value = search_return or []
    return r


def test_build_knowledge_tool_name_and_description():
    """工具名 / 描述必须稳定（Agent 通过 name 选工具）。"""
    tool = build_knowledge_tool(_mock_retriever())
    assert tool.name == "query_business_knowledge"
    assert "业务知识库" in tool.description


def test_build_knowledge_tool_no_hit_returns_hint():
    """空命中应返回提示，让 Agent 改走 SQL 工具。"""
    tool = build_knowledge_tool(_mock_retriever(search_return=[]))
    result = tool.func("复购率")
    assert "未找到相关业务知识" in result
    assert "sql_db_query" in result


def test_build_knowledge_tool_exception_returns_error():
    """retriever 抛异常时，工具返回错误提示（不抛给 Agent）。"""
    tool = build_knowledge_tool(
        _mock_retriever(side_effect=RuntimeError("Chroma 连接失败"))
    )
    result = tool.func("复购率")
    assert "⚠️ 业务知识库查询失败" in result
    assert "Chroma 连接失败" in result


def test_build_knowledge_tool_normal_returns_context_plus_sentinel():
    """正常命中应返回 format_context + sentinel 注释。"""
    retriever = _mock_retriever(search_return=[
        {
            "metadata": {"source": "biz.md", "section": "复购率"},
            "score": 0.8,
            "content": "复购率定义",
        },
    ])
    tool = build_knowledge_tool(retriever)
    result = tool.func("复购率")

    # 1) 主文本含 LLM 看的格式
    assert "复购率定义" in result
    assert "biz.md" in result

    # 2) 末尾带 sentinel
    assert _RAG_META_BEGIN in result and _RAG_META_END in result

    # 3) sentinel 可被正则解析（与 extractor 闭环）
    pat = re.compile(re.escape(_RAG_META_BEGIN) + r"(\[.*?\])" + re.escape(_RAG_META_END))
    m = pat.search(result)
    assert m is not None
    payload = json.loads(m.group(1))
    assert payload[0]["filename"] == "biz.md"
    assert payload[0]["score"] == 0.8


def test_build_knowledge_tool_passes_k_and_threshold():
    """工具应固定 k=3, score_threshold=0.4（与 retriever 默认一致）。"""
    retriever = _mock_retriever(search_return=[])
    tool = build_knowledge_tool(retriever)
    tool.func("test")
    retriever.retrieve.assert_called_once_with("test", k=3, score_threshold=0.4)
