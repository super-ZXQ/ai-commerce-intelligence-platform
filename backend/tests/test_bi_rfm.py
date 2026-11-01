"""BI 页面 RFM 纯函数：日期口径与后端 DATEDIFF 对齐。"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from bi_rfm import score_rfm_frame


def test_same_day_order_has_zero_recency():
    """参考日 10:00 的订单不能因为减零点而被算成 -1 天。"""
    orders = pd.DataFrame(
        [
            {"user_name": "u1", "order_time": "2026-01-31 10:00:00",
             "order_no": "O1", "payment_amount": 10.0},
            {"user_name": "u1", "order_time": "2026-01-31 08:00:00",
             "order_no": "O2", "payment_amount": 5.0},
            {"user_name": "u2", "order_time": "2026-01-30 23:00:00",
             "order_no": "O3", "payment_amount": 20.0},
            {"user_name": "u3", "order_time": "2026-02-01 00:01:00",
             "order_no": "O4", "payment_amount": 99.0},
            {"user_name": "u4", "order_time": "2026-01-31 09:00:00",
             "order_no": "O5", "payment_amount": 0.0},
        ]
    )

    rfm = score_rfm_frame(orders, date(2026, 1, 31), n_bins=5).set_index("user_name")

    # 参考日之后的订单与 0 元订单不进入 RFM。
    assert set(rfm.index) == {"u1", "u2"}
    assert rfm.loc["u1", "recency_days"] == 0
    assert rfm.loc["u2", "recency_days"] == 1
    assert rfm.loc["u1", "frequency"] == 2
    assert rfm.loc["u1", "monetary"] == pytest.approx(15.0)
    assert rfm.loc["u2", "frequency"] == 1


def test_missing_columns_are_rejected():
    with pytest.raises(ValueError, match="缺少列"):
        score_rfm_frame(pd.DataFrame([{"user_name": "u1"}]), date(2026, 1, 31))
