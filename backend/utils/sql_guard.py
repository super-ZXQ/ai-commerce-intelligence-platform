"""SQL 只读安全边界。

校验既用于显式执行 SQL，也挂在 SQLAlchemy Engine 上，确保 LangChain
工具在真正触达数据库前同样受到限制。
"""
from __future__ import annotations

import re

from sqlalchemy import event


class UnsafeSQLError(ValueError):
    """SQL 不是受支持的只读查询。"""


_ALLOWED_PREFIXES = {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "PRAGMA"}
# 注意：REPLACE()/TRUNCATE() 是 SELECT 中常见的只读函数，不能按关键字拦截；
# REPLACE INTO / TRUNCATE TABLE 这类语句由前缀白名单与 AST 层拦截。
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|"
    r"GRANT|REVOKE|RENAME|CALL|EXECUTE|EXEC|SET|USE|LOAD|HANDLER|LOCK|UNLOCK|"
    r"ANALYZE|OPTIMIZE|REPAIR|FLUSH|KILL|INSTALL|UNINSTALL)\b",
    re.IGNORECASE,
)
_FORBIDDEN_SELECT_PATTERNS = re.compile(
    r"\bINTO\s+(?:OUTFILE|DUMPFILE)\b|"
    r"\bFOR\s+(?:UPDATE|SHARE)\b|"
    r"\bLOCK\s+IN\s+SHARE\s+MODE\b|"
    # MySQL 8.0 锁子句修饰符，只随 FOR UPDATE/FOR SHARE 出现，一并拦截
    r"\b(?:NOWAIT|SKIP\s+LOCKED)\b|"
    r"\b(?:LOAD_FILE|SLEEP|BENCHMARK)\s*\(",
    re.IGNORECASE,
)
# SQLAlchemy Inspector 在 MySQL 上反射表结构时发出的只读元数据语句，
# 必须放行，否则 describe_table 会被 CREATE 关键字黑名单误伤
_SHOW_CREATE_METADATA = re.compile(r"\bSHOW\s+CREATE\s+(?:TABLE|VIEW)\b", re.IGNORECASE)
# SHOW 只放行结构反射与表清单；SHOW GRANTS / VARIABLES / PROCESSLIST 属于信息泄露。
_SAFE_SHOW = re.compile(
    r"^\s*SHOW\s+(?:CREATE\s+(?:TABLE|VIEW)|TABLES|FULL\s+COLUMNS|"
    r"COLUMNS|INDEX|TABLE\s+STATUS|DATABASES)\b",
    re.IGNORECASE,
)


def _strip_literals_and_comments(sql: str) -> str:
    """按 SQL 词法顺序移除注释和字面量。

    不能先用正则删除注释，因为字符串中的 ``--`` / ``/*`` 不是注释；
    若处理顺序错误，可能把后续危险语句一并隐藏。
    """
    result: list[str] = []
    i = 0
    length = len(sql)

    while i < length:
        char = sql[i]
        next_char = sql[i + 1] if i + 1 < length else ""

        if char in {"'", '"', "`"}:
            quote = char
            result.append(" ")
            i += 1
            while i < length:
                if sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == quote:
                    if i + 1 < length and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            result.append(" ")
            continue

        if char == "/" and next_char == "*":
            end = sql.find("*/", i + 2)
            i = length if end == -1 else end + 2
            result.append(" ")
            continue

        if char == "-" and next_char == "-":
            end = sql.find("\n", i + 2)
            i = length if end == -1 else end + 1
            result.append("\n")
            continue

        if char == "#":
            end = sql.find("\n", i + 1)
            i = length if end == -1 else end + 1
            result.append("\n")
            continue

        result.append(char)
        i += 1

    return "".join(result)


def is_read_only_sql(sql: str) -> bool:
    if not isinstance(sql, str) or not sql.strip():
        return False

    cleaned_all = _strip_literals_and_comments(sql)
    statements = [s.strip() for s in cleaned_all.split(";") if s.strip()]
    if len(statements) != 1:
        return False

    cleaned = statements[0]
    # AST 层会把 (SELECT ...) UNION (SELECT ...) 原样输出为以 "(" 开头的语句；
    # 先剥掉前导括号再判断前缀，避免合法只读查询在执行层被误杀。
    cleaned = cleaned.lstrip(" \t\r\n(")
    first_match = re.match(r"([A-Za-z]+)", cleaned)
    if not first_match or first_match.group(1).upper() not in _ALLOWED_PREFIXES:
        return False
    if first_match.group(1).upper() == "SHOW" and not _SAFE_SHOW.search(cleaned):
        return False
    # 先剔除 SHOW CREATE TABLE/VIEW 反射语句再做黑名单检查：
    # 其余语句（含 SHOW CREATE PROCEDURE 等）中的 CREATE 关键字依然被拦截
    cleaned = _SHOW_CREATE_METADATA.sub(" ", cleaned)
    if _FORBIDDEN_KEYWORDS.search(cleaned):
        return False
    return not _FORBIDDEN_SELECT_PATTERNS.search(cleaned)


def ensure_read_only_sql(sql: str) -> None:
    if not is_read_only_sql(sql):
        raise UnsafeSQLError("仅允许单条只读查询语句")


def guard_read_only_engine(engine) -> None:
    """给 SQLAlchemy Engine 安装幂等的执行前只读拦截器。"""
    if getattr(engine, "_read_only_guard_installed", False):
        return

    @event.listens_for(engine, "before_cursor_execute")
    def _guard(_conn, _cursor, statement, _parameters, _context, _executemany):
        ensure_read_only_sql(statement)

    engine._read_only_guard_installed = True
