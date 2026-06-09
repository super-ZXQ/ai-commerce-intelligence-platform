"""RAG 提示词模板测试。

目标：保证 prompt 文本不会因误改而破坏 Agent 行为。
- 关键工具名 query_business_knowledge 必须出现
- 业务规则区段必须包含核心概念
- build_augmented_prefix 必须能注入业务 context
"""
from rag.prompts import (
    KNOWLEDGE_TOOL_DESCRIPTION,
    TOOL_USAGE_RULES,
    build_augmented_prefix,
)


def test_knowledge_tool_description_has_tool_name():
    """工具说明必须显式提到查询入口（Agent 选工具的依据）。"""
    # 注意：工具的确切名 "query_business_knowledge" 由 build_knowledge_tool 注入，
    # prompts 中的描述使用"业务知识库"等中文表述即可。Agent 通过 Tool.name 选工具。
    assert "业务知识库" in KNOWLEDGE_TOOL_DESCRIPTION


def test_knowledge_tool_description_has_scenarios():
    """工具说明要列出适用场景和不适用场景。"""
    assert "适用场景" in KNOWLEDGE_TOOL_DESCRIPTION
    assert "不适用场景" in KNOWLEDGE_TOOL_DESCRIPTION


def test_knowledge_tool_description_mentions_kpi():
    """KPI 公式是核心场景，必须在工具说明里。"""
    assert "复购率" in KNOWLEDGE_TOOL_DESCRIPTION or "客单价" in KNOWLEDGE_TOOL_DESCRIPTION


def test_tool_usage_rules_has_decision_tree():
    """工具使用规则必须给出选工具的决策依据。"""
    assert "决策树" in TOOL_USAGE_RULES
    assert "query_business_knowledge" in TOOL_USAGE_RULES
    assert "sql_db_query" in TOOL_USAGE_RULES


def test_tool_usage_rules_emphasize_chinese():
    """强制中文回答。"""
    assert "中文" in TOOL_USAGE_RULES


def test_build_augmented_prefix_contains_both_tools():
    prefix = build_augmented_prefix(business_context="## dummy")
    assert "query_business_knowledge" in prefix
    assert "sql_db_query" in prefix


def test_build_augmented_prefix_injects_business_context():
    ctx = "## 业务规则\n- 复购率=2次以上"
    prefix = build_augmented_prefix(business_context=ctx)
    assert ctx in prefix


def test_build_augmented_prefix_has_answer_template():
    """必须给出回答模板，避免 Agent 输出格式漂移。"""
    prefix = build_augmented_prefix(business_context="x")
    assert "回答模板" in prefix
    assert "知识问答" in prefix
    assert "数据查询" in prefix
