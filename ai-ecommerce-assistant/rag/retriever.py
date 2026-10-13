"""检索器：在 VectorStore 之上增加：
- LRU + TTL 缓存（减少重复检索）
- 超时保护
- 可观测埋点（counters + score 桶分布 + 文件级 stats 共享）
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from copy import deepcopy

from . import metrics
from .vector_store import VectorStore

logger = logging.getLogger(__name__)

DEFAULT_K = 3
DEFAULT_SCORE_THRESHOLD = 0.4  # 相似度归一化后 0-1，0.4 ≈ 较相关
DEFAULT_CACHE_MAX = 128
DEFAULT_CACHE_TTL = 300  # 5 分钟

# Top1 score 桶分布，用于监控检索质量与阈值合理性
SCORE_BUCKETS = [0.2, 0.4, 0.6, 0.8, 1.0001]  # 末位 +ε 包含 1.0


def _bucketize(score: float) -> str:
    """把 score 映射到 bucket 标签。"""
    for i, edge in enumerate(SCORE_BUCKETS):
        if score < edge:
            return f"[0,{SCORE_BUCKETS[0]:.1f})" if i == 0 else (
                f"[{SCORE_BUCKETS[i-1]:.1f},{edge:.1f})" if i < len(SCORE_BUCKETS) - 1
                else f"[{SCORE_BUCKETS[i-1]:.1f},1.0]"
            )
    return "[0.8,1.0]"


class Retriever:
    """业务知识检索器。

    Usage:
        retriever = Retriever(store)
        docs = retriever.retrieve("复购率怎么算")
    """

    def __init__(self,
                 vector_store: VectorStore,
                 k: int = DEFAULT_K,
                 score_threshold: float = DEFAULT_SCORE_THRESHOLD,
                 cache_max: int = DEFAULT_CACHE_MAX,
                 cache_ttl: int = DEFAULT_CACHE_TTL,
                 stats_path: str = metrics.DEFAULT_STATS_PATH,
                 dump_interval_s: float = 5.0):
        self._store = vector_store
        self.k = k
        self.score_threshold = score_threshold
        self._cache_max = cache_max
        self._cache_ttl = cache_ttl
        self._cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
        self._lock = threading.Lock()
        self._generation = 0
        # 跨进程 stats 文件路径
        self._stats_path = stats_path
        self._dump_interval_s = dump_interval_s
        self._last_dump_ts = 0.0
        # 统计
        self.stats = {
            "cache_hits": 0,     # 命中本地缓存的次数
            "misses": 0,         # 实际调用向量库的次数
            "timeouts": 0,       # 单次检索超时次数
            "total_ms": 0.0,     # 累计检索耗时（毫秒）
            "no_results": 0,     # 实际检索但无命中
            # Top1 score 桶分布（每次 store 命中时按 top1 累加）
            "score_buckets": {b: 0 for b in (
                "[0,0.2)", "[0.2,0.4)", "[0.4,0.6)", "[0.6,0.8)", "[0.8,1.0]"
            )},
        }

    @staticmethod
    def _key(query: str, k: int, threshold: float, filter: dict | None = None, knowledge_version: str = "0") -> str:
        raw = json.dumps([query, k, threshold, filter, knowledge_version], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def invalidate_cache(self) -> None:
        with self._lock:
            self._generation += 1
            self._cache.clear()

    def retrieve(self,
                 query: str,
                 k: int | None = None,
                 score_threshold: float | None = None,
                 filter: dict | None = None,
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
        k = self.k if k is None else k
        if k < 1:
            raise ValueError("k必须大于0")
        threshold = score_threshold if score_threshold is not None else self.score_threshold
        version = str(self._store.knowledge_version)
        cache_key = self._key(query, k, threshold, filter, version)

        with self._lock:
            generation = self._generation
            # 1. 查缓存
            if cache_key in self._cache:
                ts, cached = self._cache[cache_key]
                if time.time() - ts < self._cache_ttl:
                    self._cache.move_to_end(cache_key)
                    self.stats["cache_hits"] += 1
                    logger.debug("RAG 缓存命中 question_length=%d", len(query))
                    metrics.log_event(
                        "retrieval",
                        query=query[:100],
                        top1_score=cached[0].get("score", 0.0) if cached else 0.0,
                        hits=len(cached),
                        ms=0.0,
                        cache_hit=True,
                        source="cache",
                    )
                    return deepcopy(cached)
                # 过期
                del self._cache[cache_key]

        # 2. 检索
        t0 = time.time()
        search_failed = False
        try:
            results = self._store.search(
                query, k=k, score_threshold=threshold, filter=filter
            )
        except Exception as e:
            logger.error("RAG 检索失败: %s", type(e).__name__)
            results = []
            search_failed = True
        elapsed_ms = (time.time() - t0) * 1000
        if elapsed_ms > timeout_s * 1000:
            self.stats["timeouts"] += 1
        self.stats["misses"] += 1
        self.stats["total_ms"] += elapsed_ms

        # 3. 写缓存：异常产生的空结果绝不能入缓存，否则一次瞬时故障
        #    （Chroma 抖动、重建中的空库）会被放大成 TTL 内一律"无知识命中"。
        #    正常检索出的空结果（阈值过滤后无命中）仍照常缓存。
        if not search_failed:
            with self._lock:
                if generation == self._generation and version == str(self._store.knowledge_version) and self._cache_max > 0:
                    if len(self._cache) >= self._cache_max:
                        self._cache.popitem(last=False)
                    self._cache[cache_key] = (time.time(), deepcopy(results))

        # 4. 更新 Top1 score 桶
        if results:
            top1 = results[0].get("score", 0.0)
            self.stats["score_buckets"][_bucketize(top1)] += 1
        else:
            self.stats["no_results"] += 1

        # 5. 节流落盘（多进程可见）
        self._maybe_dump()

        # 6. 结构化事件日志
        top1 = results[0].get("score", 0.0) if results else 0.0
        metrics.log_event(
            "retrieval",
            query=query[:100],  # 截断避免超长日志
            top1_score=round(top1, 4),
            hits=len(results),
            ms=round(elapsed_ms, 1),
            cache_hit=False,
            source="store",
        )

        logger.debug("RAG 检索: question_length=%d hits=%d top1=%.2f ms=%.1f",
                     len(query), len(results),
                     results[0]["score"] if results else 0, elapsed_ms)
        return results

    def get_stats(self) -> dict:
        """获取检索统计指标。

        字段说明：
        - cache_hits: 命中本地缓存的次数
        - store_hits: 实际调用向量库的次数
        - timeouts: 单次检索耗时超过 timeout_s 的次数
        - cache_size: 当前缓存条目数
        - hit_rate_pct: 缓存命中率（0-100）
        - avg_latency_ms: 平均单次实际检索耗时（毫秒）
        - no_results: 实际检索但无命中的次数
        - score_buckets: Top1 score 分布
        - total_queries: 总查询次数
        """
        with self._lock:
            cache_size = len(self._cache)
        total = self.stats["cache_hits"] + self.stats["misses"]
        hit_rate = self.stats["cache_hits"] / total * 100 if total > 0 else 0
        # store_hits 语义：实际访问向量库的次数，即 misses
        store_hits = self.stats["misses"]
        avg_ms = self.stats["total_ms"] / store_hits if store_hits > 0 else 0
        return {
            "cache_hits": self.stats["cache_hits"],
            "store_hits": store_hits,
            "timeouts": self.stats["timeouts"],
            "no_results": self.stats["no_results"],
            "cache_size": cache_size,
            "hit_rate_pct": round(hit_rate, 1),
            "avg_latency_ms": round(avg_ms, 1),
            "score_buckets": dict(self.stats["score_buckets"]),
            "total_queries": total,
        }

    # ─────────── 跨进程 stats 共享（atomic JSON 文件） ───────────

    def _maybe_dump(self) -> None:
        """节流写入：避免每次检索都 IO。"""
        now = time.time()
        if now - self._last_dump_ts < self._dump_interval_s:
            return
        try:
            self.dump_stats(self._stats_path)
            self._last_dump_ts = now
        except Exception as e:
            # 监控落盘失败不应影响主流程
            logger.warning("RAG stats 落盘失败: %s", e)

    def dump_stats(self, path: str | None = None) -> None:
        """把当前 stats（含 tools 端统计）原子写入 JSON 文件。"""
        target = path or self._stats_path
        metrics.dump_combined_stats(self.get_stats(), path=target)

