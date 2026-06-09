"""检索器：在 VectorStore 之上增加：
- LRU + TTL 缓存（减少重复检索）
- 超时保护
- 上下文格式化
- 可观测埋点
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Optional

from .vector_store import VectorStore

logger = logging.getLogger(__name__)

DEFAULT_K = 3
DEFAULT_SCORE_THRESHOLD = 0.4  # 相似度归一化后 0-1，0.4 ≈ 较相关
DEFAULT_CACHE_MAX = 128
DEFAULT_CACHE_TTL = 300  # 5 分钟


class Retriever:
    """业务知识检索器。

    Usage:
        retriever = Retriever(store)
        docs = retriever.retrieve("复购率怎么算")
        context = retriever.format_context(docs)
    """

    def __init__(self,
                 vector_store: VectorStore,
                 k: int = DEFAULT_K,
                 score_threshold: float = DEFAULT_SCORE_THRESHOLD,
                 cache_max: int = DEFAULT_CACHE_MAX,
                 cache_ttl: int = DEFAULT_CACHE_TTL):
        self._store = vector_store
        self.k = k
        self.score_threshold = score_threshold
        self._cache_max = cache_max
        self._cache_ttl = cache_ttl
        self._cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
        self._lock = threading.Lock()
        # 统计
        self.stats = {
            "hits": 0,
            "misses": 0,
            "cache_hits": 0,
            "timeouts": 0,
            "total_ms": 0.0,
        }

    @staticmethod
    def _key(query: str, k: int, threshold: float) -> str:
        raw = f"{query.strip()}|k={k}|t={threshold}"
        return hashlib.md5(raw.encode()).hexdigest()

    def retrieve(self,
                 query: str,
                 k: Optional[int] = None,
                 score_threshold: Optional[float] = None,
                 filter: Optional[dict] = None,
                 timeout_s: float = 5.0) -> list[dict]:
        """检索 Top-K 业务知识。

        Args:
            query: 用户问题。
            k: 覆盖默认 K。
            score_threshold: 覆盖默认阈值。
            filter: 元数据过滤。
            timeout_s: 软超时（实际由 Chroma 内部限制；这里只统计）。

        Returns:
            [{"content", "metadata", "score"}]
        """
        k = k or self.k
        threshold = score_threshold if score_threshold is not None else self.score_threshold
        cache_key = self._key(query, k, threshold)

        with self._lock:
            # 1. 查缓存
            if cache_key in self._cache:
                ts, cached = self._cache[cache_key]
                if time.time() - ts < self._cache_ttl:
                    self._cache.move_to_end(cache_key)
                    self.stats["hits"] += 1
                    self.stats["cache_hits"] += 1
                    logger.debug("RAG 缓存命中: %s", query[:30])
                    return cached
                # 过期
                del self._cache[cache_key]

        # 2. 检索
        t0 = time.time()
        try:
            results = self._store.search(
                query, k=k, score_threshold=threshold, filter=filter
            )
        except Exception as e:
            logger.error("RAG 检索失败: %s", e)
            results = []
        elapsed_ms = (time.time() - t0) * 1000
        if elapsed_ms > timeout_s * 1000:
            self.stats["timeouts"] += 1
        self.stats["misses"] += 1
        self.stats["total_ms"] += elapsed_ms

        # 3. 写缓存
        with self._lock:
            if len(self._cache) >= self._cache_max:
                self._cache.popitem(last=False)  # LRU 淘汰
            self._cache[cache_key] = (time.time(), results)

        logger.debug("RAG 检索: q=%r hits=%d score_range=[%.2f~%.2f] ms=%.1f",
                     query[:30], len(results),
                     results[-1]["score"] if results else 0,
                     results[0]["score"] if results else 0,
                     elapsed_ms)
        return results

    @staticmethod
    def format_context(docs: list[dict],
                       max_total_chars: int = 2400,
                       include_score: bool = False) -> str:
        """把检索结果格式化为可注入 prompt 的上下文。

        格式示例：
            【参考业务知识 1】（来源：business_glossary.md，复购率）
            复购率 = 消费 2 次及以上的用户占总用户比例...
        """
        if not docs:
            return ""
        lines = ["# 参考业务知识（来自 RAG 检索，仅供参考）"]
        total = 0
        for i, d in enumerate(docs, 1):
            md = d.get("metadata", {})
            source = md.get("source", "unknown")
            section = md.get("section", "")
            header = f"【参考 {i}】(来源: {Path(source).name}"
            if section:
                header += f", 章节: {section}"
            if include_score:
                header += f", 相关度: {d.get('score', 0):.2f}"
            header += ")"
            content = d["content"].strip()
            block = f"{header}\n{content}"
            if total + len(block) > max_total_chars:
                # 截断最后一个 block
                remain = max_total_chars - total
                if remain > 100:
                    block = block[:remain] + "\n...(已截断)"
                    lines.append(block)
                break
            lines.append(block)
            total += len(block)
        return "\n\n".join(lines)

    def get_stats(self) -> dict:
        """获取检索统计指标。

        字段说明：
        - cache_hits: 命中本地缓存的次数
        - store_hits: 实际调用向量库的次数
        - timeouts: 单次检索耗时超过 timeout_s 的次数
        - cache_size: 当前缓存条目数
        - hit_rate_pct: 缓存命中率（0-100）
        - avg_latency_ms: 平均单次实际检索耗时（毫秒）
        """
        with self._lock:
            cache_size = len(self._cache)
        total = self.stats["hits"] + self.stats["misses"]
        hit_rate = self.stats["hits"] / total * 100 if total > 0 else 0
        # store_hits 语义：实际访问向量库的次数，即 misses
        store_hits = self.stats["misses"]
        avg_ms = self.stats["total_ms"] / store_hits if store_hits > 0 else 0
        return {
            "cache_hits": self.stats["cache_hits"],
            "store_hits": store_hits,
            "timeouts": self.stats["timeouts"],
            "cache_size": cache_size,
            "hit_rate_pct": round(hit_rate, 1),
            "avg_latency_ms": round(avg_ms, 1),
        }


# 延迟导入避免循环
from pathlib import Path  # noqa: E402
