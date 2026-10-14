"""共享文本工具：跨 backend 与 ai-ecommerce-assistant 复用。"""
import re

_SQL_FENCE_OPEN = re.compile(r"```(?:sql)?\s*", re.IGNORECASE)
_SQL_FENCE_CLOSE = re.compile(r"```\s*$", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
# DSN 凭据：mysql+pymysql://user:password@host → 抹掉密码，避免 SQLAlchemy 异常把连接串带上页面
_DSN_CREDENTIALS_RE = re.compile(r"(\w+?)://([^:/\s]+):([^@/\s]+)@")


def clean_sql(sql: str, strip_html: bool = False) -> str:
    """剥离 LLM 输出中的 SQL 代码块标记（```sql ... ```）。

    Args:
        sql: 原始 SQL 文本。
        strip_html: 是否同时剥除 HTML 标签（用于处理被高亮污染的回显 SQL）。

    Returns:
        清洗后的 SQL 文本。
    """
    if not sql:
        return ""
    if strip_html:
        sql = _HTML_TAG.sub("", sql)
    sql = _SQL_FENCE_OPEN.sub("", sql)
    sql = _SQL_FENCE_CLOSE.sub("", sql)
    return sql.strip()


def sanitize_error(exc: BaseException) -> str:
    """异常文本脱敏后再展示到 UI，避免带密码的数据库连接串（DSN）外泄。

    SQLAlchemy 部分异常会携带完整 DSN（mysql+pymysql://user:password@host），
    页面只展示抹掉凭据的版本；调用端日志默认仅记录异常类别。
    """
    return _DSN_CREDENTIALS_RE.sub(r"\1://\2:***@", str(exc))
