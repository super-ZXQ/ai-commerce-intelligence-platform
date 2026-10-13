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
import tempfile
from pathlib import Path
from uuid import uuid4

from langchain_chroma import Chroma
from langchain_core.documents import Document

from .embeddings import get_embeddings

logger = logging.getLogger(__name__)

DEFAULT_PERSIST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chroma"
)
DEFAULT_COLLECTION = "business_knowledge"


def _distance_to_relevance(distance: float) -> float:
    """将非负距离稳定映射到 0-1，兼容真实与测试 Embedding。"""
    return 1.0 / (1.0 + max(float(distance), 0.0))


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
            relevance_score_fn=_distance_to_relevance,
        )
        logger.info("VectorStore 就绪: %s (collection=%s)",
                    persist_dir, collection_name)

    @property
    def knowledge_version(self) -> str:
        """共享版本文件使其他进程的检索缓存也能识别写入。"""
        try:
            return (Path(self.persist_dir) / "knowledge.version").read_text(encoding="utf-8")
        except FileNotFoundError:
            return "0"

    def _publish_version(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.persist_dir, delete=False) as file:
            file.write(uuid4().hex)
            temporary = file.name
        os.replace(temporary, Path(self.persist_dir) / "knowledge.version")

    def delete_documents(self, ids: list[str]) -> None:
        if ids:
            self._publish_version()
            try:
                self._store.delete(ids=ids)
            finally:
                self._publish_version()

    def add_documents(self,
                      documents: list[Document],
                      ids: list[str] | None = None) -> list[str]:
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
        # LangChain Chroma使用upsert；无需先删除，避免写入失败时丢失旧记录。
        self._publish_version()
        try:
            self._store.add_documents(documents=documents, ids=ids)
        finally:
            self._publish_version()
        # langchain-chroma 的 Chroma 客户端写入即自动持久化（PersistentClient），
        # 无需（也没有）手动 persist 调用。
        return ids

    def search(self,
               query: str,
               k: int = 3,
               score_threshold: float | None = None,
               filter: dict | None = None) -> list[dict]:
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
        return self._store._collection.count()

    def delete_all(self):
        """清空集合（保留 collection 结构，仅清空文档）。"""
        all_ids = self.get_all_ids()
        self.delete_documents(all_ids)
        logger.warning("⚠️ 已清空向量库（%d 条）: %s", len(all_ids), self.collection_name)

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
                relevance_score_fn=_distance_to_relevance,
            )
        except Exception as e:
            raise RuntimeError("重建向量库失败") from e

    def get_all_ids(self) -> list[str]:
        """获取全部已写入 ID（用于增量构建比对）。"""
        return self._store.get().get("ids", [])
