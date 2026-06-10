"""业务知识查询工具：作为 LangChain Tool 暴露给 Agent。

使用示例：
    from rag.tools import build_knowledge_tool
    tool = build_knowledge_tool(retriever)
    agent = create_sql_agent(..., extra_tools=[tool])

说明：
    工具返回的 observation 末尾会附带一段 HTML 注释 sentinel，
    内含结构化的检索元数据（filename / section / score / preview），
    供 app.py 抽取后渲染"参考知识"面板。注释对 LLM 透明，不影响回答。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.tools import Tool

from . import metrics
from .prompts import KNOWLEDGE_TOOL_DESCRIPTION

if TYPE_CHECKING:
    from .retriever import Retriever

logger = logging.getLogger(__name__)

# 检索元数据 sentinel，app.py 用同一字符串解析
_RAG_META_BEGIN = "<!--RAG_SOURCES:"
_RAG_META_END = "-->"


def _build_meta_sentinel(docs: list[dict]) -> str:
    """把检索结果序列化成 HTML 注释，附在工具返回末尾。

    设计要点：
    - LLM 不会主动复制 HTML 注释到最终回答（注释语义上"不可见"）
    - 注释内是 JSON，app.py 可直接 json.loads
    - 即便被 LLM 复制也不影响用户（Markdown 渲染时注释不显示）
    """
    payload = [
        {
            "rank": i + 1,
            "filename": Path(d.get("metadata", {}).get("source", "unknown")).name,
            "section": d.get("metadata", {}).get("section", ""),
            "score": round(float(d.get("score", 0.0)), 4),
            "preview": d.get("content", "").strip()[:200],
        }
        for i, d in enumerate(docs)
    ]
    return f"{_RAG_META_BEGIN}{json.dumps(payload, ensure_ascii=False)}{_RAG_META_END}"


def build_knowledge_tool(retriever: "Retriever") -> Tool:
    """构造业务知识查询工具。

    Args:
        retriever: 业务知识检索器。

    Returns:
        LangChain Tool 实例，可加入 Agent 工具列表。
    """

    def _query(query: str) -> str:
        """供 LLM 调用的实现。"""
        error: Exception | None = None
        hit = False
        try:
            docs = retriever.retrieve(query, k=3, score_threshold=0.4)
            hit = bool(docs)
        except Exception as e:
            logger.error("RAG 工具检索失败: %s", e)
            error = e
            docs = []
        # 埋点（无论命中 / 异常 / 空结果都记录）
        metrics.record_tool_call(hit=hit, error=error is not None)
        if error is not None:
            return f"⚠️ 业务知识库查询失败：{error}"
        if not docs:
            return (
                "未找到相关业务知识。"
                "如需查询具体数据，请改用 sql_db_query 工具查询 orders 表。"
            )
        # 主体：给 LLM 看的格式化上下文
        text = retriever.format_context(docs, max_total_chars=2400)
        # 附注：给 app.py 抽取的结构化元数据
        return f"{text}\n{_build_meta_sentinel(docs)}"

    return Tool(
        name="query_business_knowledge",
        func=_query,
        description=KNOWLEDGE_TOOL_DESCRIPTION,
    )
