"""RAG 来源抽取器：从 LangChain Agent 的 intermediate_steps 中还原检索结果。

职责单一、与 streamlit/app.py 解耦，便于独立单元测试。

工作原理：
    rag.tools._query 在 observation 末尾追加一段
        <!--RAG_SOURCES:[{...}, ...]-->
    注释 sentinel（结构化元数据），本模块用正则 + json 还原成
    标准的来源列表，供 UI 渲染"参考知识"面板。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 与 rag.tools 中的 sentinel 字符串保持一致
_RAG_META_BEGIN = "<!--RAG_SOURCES:"
_RAG_META_END = "-->"
_RAG_META_PATTERN = re.compile(r"<!--RAG_SOURCES:(\[.*?\])-->", re.DOTALL)

# Agent 工具名（AgentAction.tool 在 ReAct 实现中是字符串）
_KNOWLEDGE_TOOL_NAME = "query_business_knowledge"


def _action_tool_name(action: Any) -> str:
    """从 AgentAction 中安全取出 tool 字符串。"""
    tool = getattr(action, "tool", None)
    if tool is None and isinstance(action, dict):
        tool = action.get("tool")
    return str(tool or "")


def extract_rag_sources(response: dict | None) -> list[dict]:
    """从 Agent 响应中抽取 RAG 检索来源。

    Args:
        response: LangChain AgentExecutor.invoke 的返回值，
            含 "intermediate_steps": [(AgentAction, observation), ...]。

    Returns:
        来源列表，每条形如
            {"rank": int, "filename": str, "section": str,
             "score": float, "preview": str}
        若 Agent 未调用 query_business_knowledge 工具或 sentinel 解析失败，
        返回空列表（绝不抛异常）。
    """
    if not isinstance(response, dict):
        return []
    steps = response.get("intermediate_steps") or []
    if not steps:
        return []

    sources: list[dict] = []
    for step in steps:
        # ReAct 步骤通常是 (AgentAction, observation) 元组
        if not (isinstance(step, tuple) and len(step) >= 2):
            continue
        action, observation = step[0], step[1]
        if _action_tool_name(action) != _KNOWLEDGE_TOOL_NAME:
            continue
        if not isinstance(observation, str):
            continue
        match = _RAG_META_PATTERN.search(observation)
        if not match:
            continue
        try:
            payload = json.loads(match.group(1))
        except (ValueError, TypeError) as e:
            logger.warning("RAG sentinel 解析失败: %s", e)
            continue
        if isinstance(payload, list):
            sources.extend(payload)
    return sources
