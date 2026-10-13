"""API、AI与BI共用的数据时间和经营口径。"""
from __future__ import annotations

from sqlalchemy import column, select, table, text

BUSINESS_CONTEXT = """## 业务口径
- 实际数据起止时间随本次表结构附带；禁止假设2025年或固定截止日。
- 最近N天以数据最大order_date为末日，包含首尾共N个自然日：最大日期减(N-1)天至最大日期。
- 未指定时间不加日期条件；明确日期严格按请求筛选；日期使用order_date。
- 有效订单：order_status='ACTIVE' AND COALESCE(is_refund,'否')<>'是'。
- 默认销售额=有效订单SUM(payment_amount)，有效订单量=COUNT(DISTINCT order_id)，有效用户数=COUNT(DISTINCT user_name)，客单价=有效销售额/有效订单量。
- 退款率=非取消订单中退款订单数/非取消订单总数；退款条件is_refund='是' OR order_status='REFUNDED'，分母排除order_status='CANCELLED'。
- 退款金额单独统计；退款和取消不计入有效销售额。用户明确询问全部/历史/退款时按其要求，不套用默认有效订单过滤。
- 复购率=有效订单消费2次及以上用户数/有效用户数。内部可GROUP BY user_name，但不能在任何SELECT中输出user_name；使用SELECT COUNT(*) AS frequency的内部子查询。
- 标识列order_id/user_name/order_seq_id仅允许COUNT(DISTINCT 列)，不可明细输出、筛选或字符串变换；禁止SELECT *。
- 平台枚举APP、微信公众号、Web网站、其他；中文回答，仅使用真实工具结果，不编造数据。
"""

COLUMN_LABELS = {
    "order_seq_id": "订单顺序编号", "order_id": "订单号", "user_name": "用户名",
    "product_id": "商品编号", "order_amount": "订单金额", "payment_amount": "付款金额",
    "channel_id": "渠道编号", "platform_type": "平台类型", "order_time": "下单时间",
    "payment_time": "付款时间", "is_refund": "是否退款", "discount_amount": "优惠金额",
    "payment_duration_sec": "支付耗时_秒", "order_date": "下单日期", "order_hour": "下单小时",
    "weekday": "星期几", "order_status": "订单状态",
}


def describe_data_range(engine) -> str:
    with engine.connect() as connection:
        start, end = connection.execute(text("SELECT MIN(order_date), MAX(order_date) FROM orders")).one()
    return f"实际数据时间范围：{start} 至 {end}。最近N天以此最大日期为锚点；空库不得推测日期。"


def load_live_orders(engine):
    import pandas as pd

    # BI为已认证页面，保留原RFM/TOP用户功能；此读取不经过LLM，不把明细发给模型。
    orders = table("orders", *(column(name) for name in COLUMN_LABELS))
    frame = pd.read_sql_query(select(orders), engine)
    frame = frame.rename(columns=COLUMN_LABELS)
    for date_column in ("下单时间", "付款时间", "下单日期"):
        frame[date_column] = pd.to_datetime(frame[date_column])
    return active_orders_frame(frame)


def active_orders_frame(frame):
    mask = frame["是否退款"].fillna("否").ne("是")
    if "订单状态" in frame:
        mask &= frame["订单状态"].eq("ACTIVE")
    return frame.loc[mask].copy()
