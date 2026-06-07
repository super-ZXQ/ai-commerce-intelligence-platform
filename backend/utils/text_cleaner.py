"""共享文本工具：跨 backend 与 ai-ecommerce-assistant 复用。"""
import re

_SQL_FENCE_OPEN = re.compile(r"```(?:sql)?\s*", re.IGNORECASE)
_SQL_FENCE_CLOSE = re.compile(r"```\s*$", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")


def clean_sql(sql: str, strip_html: bool = False) -> str:
    """剥离 LLM 输出中的 SQL 代码块标记（```sql ... ```）。

    Args:
        sql: 原始 SQL 文本。
        strip_html: 是否同时剥除 HTML 标签（用于处理被高亮污染的回显 SQL）。

    Returns:
        清洗后的 SQL 字符串。
    """
    if not sql:
        return ""
    if strip_html:
        sql = _HTML_TAG.sub("", sql)
    sql = _SQL_FENCE_OPEN.sub("", sql)
    sql = _SQL_FENCE_CLOSE.sub("", sql)
    return sql.strip()
