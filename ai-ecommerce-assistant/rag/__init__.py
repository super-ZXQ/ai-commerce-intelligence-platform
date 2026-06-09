"""RAG 模块：业务知识检索增强。

子模块：
- embeddings:  Embedding 模型工厂（BGE-small-zh-v1.5）
- vector_store: Chroma 封装
- retriever:   检索器（缓存/超时/格式化）
- prompts:     RAG 提示词模板与工具说明
- extractor:   从 Agent intermediate_steps 还原检索来源（无 streamlit 依赖）
- tools:       业务知识查询 Tool 工厂

构建脚本：见 build_knowledge_base.py
"""
from .embeddings import get_embeddings, warm_up, DEFAULT_MODEL
from .vector_store import VectorStore, DEFAULT_PERSIST_DIR
from .retriever import Retriever
from .prompts import (
    KNOWLEDGE_TOOL_DESCRIPTION,
    TOOL_USAGE_RULES,
    build_augmented_prefix,
)
from .extractor import extract_rag_sources

__all__ = [
    "get_embeddings",
    "warm_up",
    "DEFAULT_MODEL",
    "VectorStore",
    "DEFAULT_PERSIST_DIR",
    "Retriever",
    "KNOWLEDGE_TOOL_DESCRIPTION",
    "TOOL_USAGE_RULES",
    "build_augmented_prefix",
    "extract_rag_sources",
]
