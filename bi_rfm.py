"""BI 页面 RFM 计算：与后端 rfm_service 使用同一日期与付款口径。

页面输入来自非取消订单快照；这里再按后端规则过滤付款金额和参考日，
参考日与最近下单日都归一化为日历日期后相减，保证参考日当天订单的
recency 为 0，而不是因减去零点产生 -1。
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from backend.utils.rfm_scoring import quantile_scores

_REQUIRED_COLUMNS = ("user_name", "order_time", "order_no", "payment_amount")
# order_date 是后端与 BI 共用的业务日期口径；旧快照没有该列时回退到
# order_time 归一日，保证升级前后都能算。新数据优先使用 order_date。
_OPTIONAL_DATE_COLUMN = "order_date"
_OUTPUT_COLUMNS = [
    "user_name",
    "last_order_date",
    "frequency",
    "monetary",
    "recency_days",
    "r_score",
    "f_score",
    "m_score",
]


_BUSINESS_TZ = "Asia/Shanghai"


def _business_day(value) -> pd.Timestamp:
    """把带/不带时区的参考时间统一为 Asia/Shanghai 业务日历日。

    业务日期列（order_date/order_time）是墙钟，不做 UTC 换算；只有带时区的
    输入才需要先落到 Asia/Shanghai，避免用 UTC 日期把跨天订单算错。
    """
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(_BUSINESS_TZ).tz_localize(None)
    return timestamp.normalize()


def score_rfm_frame(
    orders: pd.DataFrame,
    reference_date: date | datetime | str,
    n_bins: int = 5,
) -> pd.DataFrame:
    """计算每个客户的 R/F/M 值及分位评分。

    orders 需包含 user_name/order_time/order_no/payment_amount，调用方保证
    已排除取消订单；本函数按后端口径过滤付款金额 > 0 和参考日之后的数据。
    """
    missing = [column for column in _REQUIRED_COLUMNS if column not in orders.columns]
    if missing:
        raise ValueError(f"RFM 输入缺少列：{', '.join(missing)}")

    reference_day = _business_day(reference_date)
    columns = list(_REQUIRED_COLUMNS)
    if _OPTIONAL_DATE_COLUMN in orders.columns:
        columns.append(_OPTIONAL_DATE_COLUMN)
    frame = orders.loc[:, columns].copy()
    frame["payment_amount"] = pd.to_numeric(frame["payment_amount"], errors="coerce")
    frame["order_time"] = pd.to_datetime(frame["order_time"], errors="coerce")
    frame = frame[
        frame["user_name"].notna()
        & frame["payment_amount"].gt(0)
        & frame["order_time"].notna()
    ]
    order_times = frame["order_time"]
    if order_times.dt.tz is not None:
        order_times = order_times.dt.tz_convert(_BUSINESS_TZ).dt.tz_localize(None)
    order_time_dates = order_times.dt.normalize()
    if _OPTIONAL_DATE_COLUMN in frame.columns:
        # order_date 是后端所有日期筛选/ RFM 的规范日期；仅在缺失时回退到
        # order_time，避免两套日期在同一个分群结果里混用。
        order_dates = pd.to_datetime(frame[_OPTIONAL_DATE_COLUMN], errors="coerce")
        order_dates = order_dates.fillna(order_time_dates)
    else:
        order_dates = order_time_dates
    frame["_order_date"] = order_dates
    frame = frame[frame["_order_date"] <= reference_day]
    if frame.empty:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    rfm = (
        frame.groupby("user_name", as_index=False)
        .agg(
            last_order_date=("_order_date", "max"),
            frequency=("order_no", "nunique"),
            monetary=("payment_amount", "sum"),
        )
    )
    # 与后端 SUM(DECIMAL) 后再 round(2) 对齐，避免浮点尾差改变分位边界。
    rfm["monetary"] = rfm["monetary"].round(2)
    rfm["recency_days"] = (reference_day - rfm["last_order_date"]).dt.days

    for prefix, column in (("r", "recency_days"), ("f", "frequency"), ("m", "monetary")):
        rfm[f"{prefix}_score"] = quantile_scores(rfm[column].tolist(), n_bins)

    return rfm[_OUTPUT_COLUMNS]
