import logging
import math
from typing import Optional

import numpy as np
from scipy import stats as sp_stats
from sqlalchemy import select, func, case, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database_models import Order

logger = logging.getLogger(__name__)

PLATFORM_MAP = {
    "APP": "APP",
    "WECHAT_MP": "微信公众号",
    "WEB": "Web网站",
    "TAOBAO": "淘宝",
    "WECHAT_SHOP": "微信小商店",
    "WAP": "wap网站",
}

METRIC_MAP = {
    "payment_amount": "付款金额",
    "order_amount": "订单金额",
    "discount_amount": "优惠金额",
}

GROUP_DIMENSIONS = ["platform_type", "is_refunded", "weekday"]


async def get_available_groups(db: AsyncSession, dimension: str) -> dict:
    if dimension == "platform_type":
        stmt = select(Order.platform_type, func.count(Order.id)).group_by(Order.platform_type)
    elif dimension == "is_refunded":
        stmt = select(Order.is_refunded, func.count(Order.id)).group_by(Order.is_refunded)
    elif dimension == "weekday":
        stmt = select(Order.weekday, func.count(Order.id)).group_by(Order.weekday)
    else:
        return {"dimension": dimension, "groups": []}

    rows = (await db.execute(stmt)).all()
    groups = []
    for row in rows:
        label = row[0]
        if dimension == "platform_type":
            label = PLATFORM_MAP.get(row[0], row[0])
        elif dimension == "is_refunded":
            label = "已退款" if row[0] == "是" else "未退款"
        groups.append({"value": row[0], "label": label, "count": row[1]})

    return {"dimension": dimension, "groups": groups}


async def run_ab_test(
    db: AsyncSession,
    dimension: str,
    group_a_value: str,
    group_b_value: str,
    metric: str = "payment_amount",
    alpha: float = 0.05,
) -> dict:
    col = getattr(Order, metric, Order.payment_amount)

    stmt_a = select(col).where(getattr(Order, dimension) == group_a_value)
    stmt_b = select(col).where(getattr(Order, dimension) == group_b_value)

    rows_a = [float(r[0] or 0) for r in (await db.execute(stmt_a)).all()]
    rows_b = [float(r[0] or 0) for r in (await db.execute(stmt_b)).all()]

    if len(rows_a) < 2 or len(rows_b) < 2:
        return {"error": "样本量不足，每组至少需要2条数据", "group_a_count": len(rows_a), "group_b_count": len(rows_b)}

    arr_a = np.array(rows_a, dtype=np.float64)
    arr_b = np.array(rows_b, dtype=np.float64)

    desc_a = _describe(arr_a)
    desc_b = _describe(arr_b)

    t_result = _welch_ttest(arr_a, arr_b)
    mw_result = _mann_whitney(arr_a, arr_b)

    conversion_a = await _conversion_rate(db, dimension, group_a_value)
    conversion_b = await _conversion_rate(db, dimension, group_b_value)
    chi_result = _chi_square_test(conversion_a, conversion_b)

    cohens_d = _cohens_d(arr_a, arr_b)
    cramers_v = _cramers_v(chi_result["chi2_statistic"], conversion_a, conversion_b)

    ci_result = _mean_diff_ci(arr_a, arr_b, alpha)

    metric_label = METRIC_MAP.get(metric, metric)
    dim_label = {"platform_type": "平台", "is_refunded": "退款状态", "weekday": "星期"}.get(dimension, dimension)
    label_a = group_a_value
    label_b = group_b_value
    if dimension == "platform_type":
        label_a = PLATFORM_MAP.get(group_a_value, group_a_value)
        label_b = PLATFORM_MAP.get(group_b_value, group_b_value)
    elif dimension == "is_refunded":
        label_a = "已退款" if group_a_value == "是" else "未退款"
        label_b = "已退款" if group_b_value == "是" else "未退款"

    t_significant = t_result["p_value"] < alpha
    chi_significant = chi_result["p_value"] < alpha

    return {
        "experiment": {
            "dimension": dimension,
            "dimension_label": dim_label,
            "metric": metric,
            "metric_label": metric_label,
            "alpha": alpha,
            "group_a": {"value": group_a_value, "label": label_a},
            "group_b": {"value": group_b_value, "label": label_b},
        },
        "descriptive": {
            "group_a": desc_a,
            "group_b": desc_b,
        },
        "conversion": {
            "group_a": conversion_a,
            "group_b": conversion_b,
        },
        "t_test": {**t_result, "significant": t_significant},
        "mann_whitney": mw_result,
        "chi_square": {**chi_result, "significant": chi_significant},
        "effect_size": {
            "cohens_d": cohens_d,
            "cramers_v": cramers_v,
        },
        "confidence_interval": ci_result,
        "conclusion": _build_conclusion(
            t_significant, chi_significant, t_result, chi_result,
            cohens_d, cramers_v, ci_result, label_a, label_b, metric_label,
        ),
    }


async def run_multi_group(
    db: AsyncSession,
    dimension: str,
    metric: str = "payment_amount",
    alpha: float = 0.05,
) -> dict:
    col = getattr(Order, metric, Order.payment_amount)
    dim_col = getattr(Order, dimension)

    stmt = select(dim_col, col)
    rows = (await db.execute(stmt)).all()

    groups_data = {}
    for row in rows:
        key = row[0]
        val = float(row[1] or 0)
        groups_data.setdefault(key, []).append(val)

    if len(groups_data) < 2:
        return {"error": "至少需要2个分组", "group_count": len(groups_data)}

    arrays = {k: np.array(v, dtype=np.float64) for k, v in groups_data.items()}
    descriptive = {}
    for k, arr in arrays.items():
        label = k
        if dimension == "platform_type":
            label = PLATFORM_MAP.get(k, k)
        elif dimension == "is_refunded":
            label = "已退款" if k == "是" else "未退款"
        descriptive[k] = {**_describe(arr), "label": label}

    group_arrays = list(arrays.values())
    f_stat, p_value = sp_stats.f_oneway(*group_arrays)

    return {
        "dimension": dimension,
        "metric": metric,
        "metric_label": METRIC_MAP.get(metric, metric),
        "alpha": alpha,
        "group_count": len(groups_data),
        "descriptive": descriptive,
        "anova": {
            "f_statistic": round(float(f_stat), 4),
            "p_value": round(float(p_value), 6),
            "significant": float(p_value) < alpha,
        },
        "conclusion": _build_anova_conclusion(float(p_value), alpha, dimension),
    }


def _describe(arr: np.ndarray) -> dict:
    return {
        "n": int(len(arr)),
        "mean": round(float(np.mean(arr)), 4),
        "std": round(float(np.std(arr, ddof=1)), 4),
        "median": round(float(np.median(arr)), 4),
        "min": round(float(np.min(arr)), 4),
        "max": round(float(np.max(arr)), 4),
        "q1": round(float(np.percentile(arr, 25)), 4),
        "q3": round(float(np.percentile(arr, 75)), 4),
    }


def _welch_ttest(arr_a: np.ndarray, arr_b: np.ndarray) -> dict:
    t_stat, p_value = sp_stats.ttest_ind(arr_a, arr_b, equal_var=False)
    n_a, n_b = len(arr_a), len(arr_b)
    s_a, s_b = np.var(arr_a, ddof=1), np.var(arr_b, ddof=1)
    df_num = (s_a / n_a + s_b / n_b) ** 2
    df_den = (s_a / n_a) ** 2 / (n_a - 1) + (s_b / n_b) ** 2 / (n_b - 1)
    df = df_num / df_den if df_den > 0 else 0
    return {
        "t_statistic": round(float(t_stat), 4),
        "p_value": round(float(p_value), 6),
        "df": round(float(df), 4),
        "test_type": "Welch's t-test (双样本独立t检验)",
    }


def _mann_whitney(arr_a: np.ndarray, arr_b: np.ndarray) -> dict:
    u_stat, p_value = sp_stats.mannwhitneyu(arr_a, arr_b, alternative="two-sided")
    return {
        "u_statistic": round(float(u_stat), 4),
        "p_value": round(float(p_value), 6),
        "test_type": "Mann-Whitney U 检验 (非参数)",
    }


async def _conversion_rate(db: AsyncSession, dimension: str, value: str) -> dict:
    dim_col = getattr(Order, dimension)
    total_stmt = select(func.count(Order.id)).where(dim_col == value)
    paid_stmt = select(func.count(Order.id)).where(
        and_(dim_col == value, Order.is_refunded == "否")
    )
    total = (await db.execute(total_stmt)).scalar() or 0
    paid = (await db.execute(paid_stmt)).scalar() or 0
    rate = round(paid / total * 100, 4) if total > 0 else 0
    return {"total": total, "paid": paid, "rate": rate}


def _chi_square_test(conv_a: dict, conv_b: dict) -> dict:
    table = np.array([
        [conv_a["paid"], conv_a["total"] - conv_a["paid"]],
        [conv_b["paid"], conv_b["total"] - conv_b["paid"]],
    ])
    if np.any(table == 0):
        return {
            "chi2_statistic": 0.0,
            "p_value": 1.0,
            "df": 1,
            "test_type": "卡方检验 (Chi-square)",
            "note": "期望频次为0，无法执行卡方检验",
        }
    chi2, p_value, dof, expected = sp_stats.chi2_contingency(table, correction=False)
    return {
        "chi2_statistic": round(float(chi2), 4),
        "p_value": round(float(p_value), 6),
        "df": int(dof),
        "test_type": "卡方检验 (Chi-square)",
    }


def _cohens_d(arr_a: np.ndarray, arr_b: np.ndarray) -> dict:
    n_a, n_b = len(arr_a), len(arr_b)
    mean_diff = float(np.mean(arr_a) - np.mean(arr_b))
    var_a = float(np.var(arr_a, ddof=1))
    var_b = float(np.var(arr_b, ddof=1))
    pooled_std = math.sqrt(((n_a - 1) * var_a + (n_b - 1) * var_b) / (n_a + n_b - 2)) if (n_a + n_b - 2) > 0 else 0
    d = mean_diff / pooled_std if pooled_std > 0 else 0
    magnitude = "忽略" if abs(d) < 0.2 else "小" if abs(d) < 0.5 else "中" if abs(d) < 0.8 else "大"
    return {"value": round(d, 4), "magnitude": magnitude, "interpretation": f"Cohen's d = {round(d, 4)} ({magnitude}效应)"}


def _cramers_v(chi2: float, conv_a: dict, conv_b: dict) -> dict:
    n = conv_a["total"] + conv_b["total"]
    k = 2
    v = math.sqrt(chi2 / (n * (k - 1))) if n > 0 and k > 1 else 0
    magnitude = "弱" if v < 0.1 else "中" if v < 0.3 else "强"
    return {"value": round(v, 4), "magnitude": magnitude, "interpretation": f"Cramer's V = {round(v, 4)} ({magnitude}关联)"}


def _mean_diff_ci(arr_a: np.ndarray, arr_b: np.ndarray, alpha: float) -> dict:
    n_a, n_b = len(arr_a), len(arr_b)
    mean_diff = float(np.mean(arr_a) - np.mean(arr_b))
    se = math.sqrt(float(np.var(arr_a, ddof=1)) / n_a + float(np.var(arr_b, ddof=1)) / n_b)
    z = sp_stats.norm.ppf(1 - alpha / 2)
    margin = z * se
    lower = mean_diff - margin
    upper = mean_diff + margin
    return {
        "mean_diff": round(mean_diff, 4),
        "standard_error": round(se, 4),
        "confidence_level": f"{int((1 - alpha) * 100)}%",
        "lower": round(lower, 4),
        "upper": round(upper, 4),
        "interpretation": f"均值差的{int((1 - alpha) * 100)}%置信区间: [{round(lower, 4)}, {round(upper, 4)}]",
    }


def _build_conclusion(
    t_sig: bool, chi_sig: bool, t_result: dict, chi_result: dict,
    cohens_d: dict, cramers_v: dict, ci: dict,
    label_a: str, label_b: str, metric_label: str,
) -> dict:
    parts = []

    if t_sig:
        direction = "高于" if ci["mean_diff"] > 0 else "低于"
        parts.append(
            f"在{metric_label}上，{label_a}显著{direction}{label_b}（t={t_result['t_statistic']}, p={t_result['p_value']}）"
        )
    else:
        parts.append(f"在{metric_label}上，{label_a}与{label_b}无显著差异（p={t_result['p_value']}）")

    if chi_sig:
        parts.append(f"转化率存在显著差异（χ²={chi_result['chi2_statistic']}, p={chi_result['p_value']}）")
    else:
        parts.append(f"转化率无显著差异（p={chi_result['p_value']}）")

    parts.append(f"效应量：{cohens_d['interpretation']}，关联强度：{cramers_v['interpretation']}")

    if t_sig and ci["mean_diff"] > 0:
        parts.append(f"建议：{label_a}表现更优，可考虑加大该方向投入")
    elif t_sig and ci["mean_diff"] < 0:
        parts.append(f"建议：{label_b}表现更优，可考虑优化{label_a}策略")
    else:
        parts.append("建议：差异不显著，建议扩大样本量或调整实验设计")

    return {
        "summary": " | ".join(parts),
        "t_test_significant": t_sig,
        "chi_square_significant": chi_sig,
        "recommendation": parts[-1],
    }


def _build_anova_conclusion(p_value: float, alpha: float, dimension: str) -> dict:
    dim_label = {"platform_type": "平台", "is_refunded": "退款状态", "weekday": "星期"}.get(dimension, dimension)
    if p_value < alpha:
        return {
            "summary": f"不同{dim_label}间存在显著差异（p={round(p_value, 6)} < {alpha}）",
            "significant": True,
            "recommendation": f"建议进一步做两两对比（Tukey HSD），找出具体哪些{dim_label}间差异显著",
        }
    return {
        "summary": f"不同{dim_label}间无显著差异（p={round(p_value, 6)} >= {alpha}）",
        "significant": False,
        "recommendation": "差异不显著，可能需要更多数据或调整分组维度",
    }
