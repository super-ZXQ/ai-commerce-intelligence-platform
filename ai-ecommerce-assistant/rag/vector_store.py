"""Chroma 向量库封装。

特性：
- 本地持久化（persist_directory）
- 增量构建（按 ID 去重）
- 相似度阈值过滤
- 元数据过滤支持
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document

from .embeddings import get_embeddings

logger = logging.getLogger(__name__)

DEFAULT_PERSIST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chroma"
)
DEFAULT_COLLECTION = "business_knowledge"


class VectorStore:
    """业务知识向量库。

    Usage:
        store = VectorStore()  # 使用默认配置
        store.add_documents(docs, ids=[...])
        results = store.search("复购率", k=3)
    """

    def __init__(self,
                 persist_dir: str = DEFAULT_PERSIST_DIR,
                 collection_name: str = DEFAULT_COLLECTION,
                 embedding=None):
        self.persist_dir = persist_dir
        self.collection_name = collection_name
        self._embeddings = embedding or get_embeddings()
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        self._store = Chroma(
            collection_name=collection_name,
            embedding_function=self._embeddings,
            persist_directory=persist_dir,
        )
        logger.info("VectorStore 就绪: %s (collection=%s)",
                    persist_dir, collection_name)

    def add_documents(self,
                      documents: list[Document],
                      ids: Optional[list[str]] = None) -> list[str]:
        """添加文档，重复 ID 自动覆盖。

        Args:
            documents: LangChain Document 列表。
            ids: 与 documents 等长的 ID 列表；不传则用 metadata.doc_id 兜底。

        Returns:
            最终写入的 ID 列表。
        """
        if not documents:
            return []
        if ids is None:
            ids = [d.metadata.get("doc_id") or f"auto-{i}" for i, d in enumerate(documents)]
        # Chroma 的 add(ids=...) 在 ID 重复时会抛 InvalidCollectionAPIError，
        # 需先删除旧记录
        try:
            existing = self._store.get(ids=ids).get("ids", [])
            if existing:
                self._store.delete(ids=existing)
        except Exception:
            pass
        self._store.add_documents(documents=documents, ids=ids)
        try:
            self._store.persist()
        except Exception:
            pass
        return ids

    def search(self,
               query: str,
               k: int = 3,
               score_threshold: Optional[float] = None,
               filter: Optional[dict] = None) -> list[dict]:
        """检索 top-k 文档。

        Args:
            query: 查询文本。
            k: 返回数量。
            score_threshold: 相似度阈值（距离），< 阈值才返回。
                Chroma 默认 L2 距离，< 1.0 通常较相似。
            filter: 元数据过滤，例如 {"doc_type": "kpi"}。

        Returns:
            [{"content", "metadata", "score"}] 列表。
        """
        if self.count() == 0:
            return []
        # 优先用 relevance_scores（Chroma 内部做 1/(1+distance) 归一化，结果 0-1）。
        # 旧版 fallback 用 1.0 - distance 归一化，在 L2 距离 >1 时产生负数，已废弃。
        scored = self._store.similarity_search_with_relevance_scores(
            query, k=k, filter=filter
        )

        results = []
        for doc, score in scored:
            if score_threshold is not None and score < score_threshold:
                continue
            results.append({
                "content": doc.page_content,
                "metadata": doc.metadata,
                "score": float(score),
            })
        return results

    def count(self) -> int:
        try:
            return self._store._collection.count()
        except Exception:
            return 0

    def delete_all(self):
        """清空集合（保留 collection 结构，仅清空文档）。"""
        try:
            # 直接通过 collection API 删全部，比 reset() 物理删文件更安全
            all_ids = self.get_all_ids()
            if all_ids:
                self._store.delete(ids=all_ids)
            logger.warning("⚠️ 已清空向量库（%d 条）: %s", len(all_ids), self.collection_name)
        except Exception as e:
            logger.error("清空失败: %s", e)

    def reset(self, force_physical: bool = False):
        """清空向量库。

        Args:
            force_physical: True 时物理删除持久化目录（危险，文件锁可能失败）。
                            默认仅 delete_all()，保留 collection 结构。
        """
        if force_physical:
            try:
                # 先尝试 delete_collection() 释放 SQLite 锁
                try:
                    self._store.delete_collection()
                except Exception:
                    pass
                if Path(self.persist_dir).exists():
                    shutil.rmtree(self.persist_dir, ignore_errors=True)
            except Exception as e:
                logger.warning("物理删除失败（可能文件被锁）: %s", e)
        else:
            self.delete_all()
        Path(self.persist_dir).mkdir(parents=True, exist_ok=True)
        # 重新初始化 store 句柄（collection 可能已被 delete）
        try:
            self._store = Chroma(
                collection_name=self.collection_name,
                embedding_function=self._embeddings,
                persist_directory=self.persist_dir,
            )
        except Exception as e:
            logger.error("重建 store 失败: %s", e)

    def get_all_ids(self) -> list[str]:
        """获取全部已写入 ID（用于增量构建比对）。"""
        try:
            return self._store.get().get("ids", [])
        except Exception:
            return []
