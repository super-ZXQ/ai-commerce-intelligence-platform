"""RFM 分位评分工具。

所有维度均按数值从低到高评分为 1..n_bins：
- Recency 低分更优；
- Frequency / Monetary 高分更优。
"""
from __future__ import annotations

import bisect
import math
from collections.abc import Sequence

# 分群的业务归组本身就是指标口径的一部分，必须与分群规则放在同一处：
# BI 看板、/api/rfm/*、导出与 AI 分析共用，避免同一个"高价值客户"给出两个数字。
# 高价值不能按"重要"前缀子串判定——重要挽留客户属于流失预警，
# 一般价值客户反而是真实的高贡献群体。
HIGH_VALUE_SEGMENTS = frozenset({"重要价值客户", "重要发展客户", "重要保持客户"})
AT_RISK_SEGMENTS = frozenset({"重要挽留客户"})


def quantile_scores(values: Sequence[float], n_bins: int = 5) -> list[int]:
    """对输入值进行稳定的分位评分，并保持原始顺序。

    使用分位边界而不是 ``qcut``，避免大量并列值触发重复边界后，
    实际分箱数与标签缩放逻辑不一致。
    """
    if not values:
        return []

    sorted_values = sorted(values)
    count = len(sorted_values)
    thresholds = [
        sorted_values[min(math.ceil(count * index / n_bins) - 1, count - 1)]
        for index in range(1, n_bins + 1)
    ]
    return [
        min(bisect.bisect_left(thresholds, value) + 1, n_bins)
        for value in values
    ]


def assign_segment(
    r_score: int,
    f_score: int,
    m_score: int,
    r_threshold: int,
    f_threshold: int,
    m_threshold: int,
) -> str:
    """按 R 低分优、F/M 高分优的语义映射八类客户。"""
    r_high = r_score <= r_threshold
    f_high = f_score >= f_threshold
    m_high = m_score >= m_threshold

    if r_high and f_high and m_high:
        return "重要价值客户"
    if r_high and f_high and not m_high:
        return "重要发展客户"
    if r_high and not f_high and m_high:
        return "重要保持客户"
    if r_high and not f_high and not m_high:
        return "重要挽留客户"
    if not r_high and f_high and m_high:
        return "一般价值客户"
    if not r_high and f_high and not m_high:
        return "一般发展客户"
    if not r_high and not f_high and m_high:
        return "一般保持客户"
    return "一般挽留客户"
