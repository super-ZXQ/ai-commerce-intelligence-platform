"""退款率变化分析的受限工具：固定聚合 SQL 和明确的时间窗口。"""
from __future__ import annotations

import re

TOOLS = ("overall", "platform", "product")
LABELS = {"overall": "整体退款率对比", "platform": "按平台对比", "product": "按商品对比"}


def supports_refund_analysis(query: str) -> bool:
    """只处理未指定具体时段的归因问题；其他问题保留原有查询路径。"""
    return (
        "退款率" in query
        and bool(re.search(r"为什么|为何|原因|归因|怎么回事", query))
        and not re.search(
            r"\d|今年|去年|前年|本月|上月|本周|上周|本季度|上季度|今天|昨天|前天|"
            r"(?:最近|过去|近|前)[一二三四五六七八九十两]+(?:天|日|周|个月|月|年)",
            query,
        )
    )


def refund_sql(tool: str, dialect: str) -> str:
    """从固定工具生成聚合查询，窗口锚定订单数据最新日期。"""
    if tool not in TOOLS:
        raise ValueError("未知分析工具")
    if dialect == "mysql":
        current_start = "DATE_SUB(b.end_date, INTERVAL 29 DAY)"
        previous_start = "DATE_SUB(b.end_date, INTERVAL 59 DAY)"
        previous_end = "DATE_SUB(b.end_date, INTERVAL 30 DAY)"
    elif dialect == "sqlite":
        current_start = "DATE(b.end_date, '-29 days')"
        previous_start = "DATE(b.end_date, '-59 days')"
        previous_end = "DATE(b.end_date, '-30 days')"
    else:
        raise ValueError("退款分析暂不支持该数据库方言")
    dimension = {"overall": "", "platform": "o.platform_type,", "product": "o.product_id,"}[tool]
    group = {
        "overall": "GROUP BY b.end_date",
        "platform": "GROUP BY o.platform_type, b.end_date",
        "product": "GROUP BY o.product_id, b.end_date",
    }[tool]
    # 计数同时返回分子和分母，让调用方能够核验变化而非只看到模型结论。
    return f"""SELECT {dimension}
        b.end_date AS current_end, {current_start} AS current_start,
        {previous_start} AS previous_start, {previous_end} AS previous_end,
        COUNT(DISTINCT CASE WHEN o.order_date >= {current_start} THEN o.order_id END) AS current_orders,
        COUNT(DISTINCT CASE WHEN o.order_date >= {current_start} AND o.is_refund = '是' THEN o.order_id END) AS current_refunds,
        COUNT(DISTINCT CASE WHEN o.order_date < {current_start} THEN o.order_id END) AS previous_orders,
        COUNT(DISTINCT CASE WHEN o.order_date < {current_start} AND o.is_refund = '是' THEN o.order_id END) AS previous_refunds
        FROM orders AS o CROSS JOIN (SELECT MAX(order_date) AS end_date FROM orders) AS b
        WHERE o.order_date >= {previous_start} AND o.order_date <= b.end_date
        {group} ORDER BY current_refunds DESC LIMIT 50"""
