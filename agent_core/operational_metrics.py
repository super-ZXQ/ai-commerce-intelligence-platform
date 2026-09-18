"""Agent 降级计数，不记录查询文本、SQL 或凭据。"""

from __future__ import annotations

from collections import Counter

_llm_failures: Counter[str] = Counter()
_rag_failures: Counter[str] = Counter()


def record_llm_failure(category: str) -> None:
    _llm_failures[category] += 1


def record_rag_failure(category: str) -> None:
    _rag_failures[category] += 1


def snapshot() -> dict[str, dict[str, int]]:
    return {"llm_failures": dict(_llm_failures), "rag_failures": dict(_rag_failures)}
