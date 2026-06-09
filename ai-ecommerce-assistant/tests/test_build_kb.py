"""build_knowledge_base 切分函数测试。

不涉及 Chroma，仅测纯函数：_doc_type / _make_doc_id / 切分逻辑。
"""
from __future__ import annotations

import pytest

from build_knowledge_base import (
    _doc_type,
    _make_doc_id,
    _split_by_sections,
    _split_long_section,
    _sliding_window,
)


# ─────────────────── _doc_type ───────────────────


@pytest.mark.parametrize("filename,expected", [
    ("business_glossary.md", "glossary"),
    ("data_dictionary.md", "data_dict"),
    ("kpi_formulas.md", "kpi"),
    ("gold_queries.md", "gold_query"),
    ("business_rules.md", "business_rule"),
    ("api_docs.md", "api_doc"),
])
def test_doc_type_known_files(filename, expected):
    assert _doc_type(filename) == expected


def test_doc_type_unknown():
    assert _doc_type("random.md") == "other"


def test_doc_type_case_insensitive():
    assert _doc_type("KPI_FORMULAS.MD") == "kpi"


# ─────────────────── _make_doc_id ───────────────────


def test_doc_id_deterministic():
    a = _make_doc_id("a.md", "S", "content")
    b = _make_doc_id("a.md", "S", "content")
    assert a == b


def test_doc_id_length_is_16():
    """截断到 16 字符（64 bit 哈希）。"""
    a = _make_doc_id("a.md", "S", "content")
    assert len(a) == 16


def test_doc_id_differs_on_content():
    assert _make_doc_id("a.md", "S", "x") != _make_doc_id("a.md", "S", "y")


def test_doc_id_differs_on_section():
    assert _make_doc_id("a.md", "S1", "x") != _make_doc_id("a.md", "S2", "x")


# ─────────────────── _split_by_sections ───────────────────


def test_split_no_heading_returns_full_text():
    sections = list(_split_by_sections("just a paragraph\nno headings here"))
    assert len(sections) == 1
    assert sections[0][0] == ""
    assert "just a paragraph" in sections[0][1]


def test_split_with_h2_yields_each_section():
    text = """# Top

preamble

## Section A
content of A

## Section B
content of B
"""
    sections = list(_split_by_sections(text))
    titles = [t for t, _ in sections]
    assert "前言/概述" in titles
    assert "Section A" in titles
    assert "Section B" in titles


def test_split_skips_empty_sections():
    """内容为空的 H2 章节应被跳过。"""
    text = """## A
content A

## B

## C
content C
"""
    sections = list(_split_by_sections(text))
    titles = [t for t, _ in sections]
    assert "A" in titles
    assert "C" in titles
    # B 因为 content 为空，不会出现在结果中
    assert "B" not in titles


# ─────────────────── _split_long_section ───────────────────


def test_split_long_section_short_no_split():
    chunks = _split_long_section("short content", max_chars=100, overlap=10)
    assert chunks == ["short content"]


def test_split_long_section_respects_max_chars():
    content = "a" * 1000
    chunks = _split_long_section(content, max_chars=100, overlap=20)
    for c in chunks:
        # 允许 +overlap 余量
        assert len(c) <= 130


def test_split_long_section_with_h3_preserves_boundaries():
    """H3 标题应作为自然切分点。"""
    # 构造足够长的内容，触发 H3 切片
    content = (
        "intro " + ("x" * 200) + "\n"
        "### A\n" + ("a" * 200) + "\n"
        "### B\n" + ("b" * 200) + "\n"
    )
    chunks = _split_long_section(content, max_chars=120, overlap=20)
    # 至少应该切出几块
    assert len(chunks) >= 2
    # 每块不应跨越 H3
    joined = " | ".join(chunks)
    assert "### A" in joined
    assert "### B" in joined


# ─────────────────── _sliding_window ───────────────────


def test_sliding_window_short_returns_single_chunk():
    chunks = _sliding_window("abc", max_chars=100, overlap=10)
    assert chunks == ["abc"]


def test_sliding_window_has_overlap():
    """相邻 chunk 应有重叠区。"""
    text = "abcdefghij" * 30  # 300 chars
    chunks = _sliding_window(text, max_chars=50, overlap=15)
    assert len(chunks) >= 3
    # 第 1 块末尾与第 2 块开头应有重叠
    assert chunks[0][-15:] in chunks[1]


def test_sliding_window_covers_full_text():
    """所有 chunk 拼接应覆盖原文本（容许轻微差异）。"""
    text = "x" * 500
    chunks = _sliding_window(text, max_chars=100, overlap=20)
    # 拼接长度应接近原文本长度
    total = sum(len(c) for c in chunks)
    assert total >= len(text) - 30  # 允许 30 字符的截断/重复
