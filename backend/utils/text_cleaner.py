"""共享文本工具：跨 backend 与 ai-ecommerce-assistant 复用。"""
import re

_SQL_FENCE_OPEN = re.compile(r"```(?:sql)?\s*", re.IGNORECASE)
_SQL_FENCE_CLOSE = re.compile(r"```\s*$", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
# DSN 凭据：mysql+pymysql://user:password@host → 抹掉密码，避免 SQLAlchemy 异常把连接串带上页面
_DSN_CREDENTIALS_RE = re.compile(r"(\w+?)://([^:/\s]+):([^@/\s]+)@")
# 常见凭据形态：模型密钥、Bearer、请求头与查询串中的 key=value。
_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"), r"\1***"),
    (re.compile(r"(?i)(x-api-key\s*:\s*)[^\s,;]+"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "***"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|password|passwd|secret)"
            r"(\s*[=:]\s*)([^\s&,;]+)"
        ),
        r"\1\2***",
    ),
)


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
    """异常文本脱敏后再展示到 UI/日志，避免凭据外泄。

    覆盖数据库 DSN、Authorization Bearer、x-api-key、sk-* 模型密钥以及
    password/api_key/token/secret 形式的键值对。脱敏只做文本替换，不改变
    异常类型与主流程。
    """
    text = _DSN_CREDENTIALS_RE.sub(r"\1://\2:***@", str(exc))
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
