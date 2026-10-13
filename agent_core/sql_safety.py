"""基于SQL AST的只读校验与结果行数限制。"""

from __future__ import annotations

from sqlglot import exp, parse, parse_one
from sqlglot.errors import OptimizeError, ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

# 标识字段只能计数，不能作为明细输出或筛选参数；个人级分组仅允许内部计数。
IDENTIFIER_COLUMNS = {"user_name", "order_id", "order_seq_id"}
ORDER_COLUMNS = {
    "order_seq_id", "order_id", "user_name", "product_id", "order_amount",
    "payment_amount", "channel_id", "platform_type", "order_time", "payment_time",
    "is_refund", "discount_amount", "payment_duration_sec", "order_date",
    "order_hour", "weekday", "order_status", "event_version", "updated_at",
}
_SAFE_FUNCTIONS = {
    "AND", "OR", "COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND", "ABS", "CEIL", "FLOOR",
    "COALESCE", "NULLIF", "IF", "IFNULL", "CASE", "CAST", "TRY_CAST", "EXTRACT",
    "DATE", "DATE_ADD", "DATE_SUB", "DATEDIFF", "TIMESTAMPDIFF", "DATE_FORMAT",
    "TIME_TO_STR", "TS_OR_DS_TO_DATE", "TS_OR_DS_TO_TIMESTAMP", "YEAR", "MONTH", "DAY", "DAY_OF_MONTH", "DAY_OF_WEEK",
    "HOUR", "CURRENT_DATE", "CURRENT_TIMESTAMP", "STR_TO_DATE", "STR_TO_TIME",
    "LAG", "LEAD", "ROW_NUMBER", "RANK", "DENSE_RANK", "LOWER", "UPPER",
}
_SAFE_NODES = set("""
select union subquery with cte from table tablealias identifier column alias
literal null boolean star distinct where group having order ordered limit offset
join on and or not eq neq gt gte lt lte is in between like ilike exists paren
add sub mul div intdiv mod neg case when if tuple interval datatype datatypeparam
window windowspec var placeholder
""".split())

# MySQL 优化器提示：由数据库自己在超时后中断查询（5.7.8+ / 8.0 支持）。
_MYSQL_TIMEOUT_HINT = "MAX_EXECUTION_TIME"


class SQLValidationError(ValueError):
    pass


def validate_and_limit_sql(
    sql: str,
    max_rows: int = 500,
    allowed_tables: tuple[str, ...] = ("orders",),
    max_joins: int = 3,
) -> str:
    """校验只读性并改写 LIMIT；JOIN 数量超限一律拒绝。

    max_joins 不只是"复杂度"限制：模型可能生成多表笛卡尔积，而 LIMIT 只裁剪
    最终输出行数，中间结果仍然要被完整算完，因此必须在 AST 层提前拦掉。
    """
    if not isinstance(sql, str) or len(sql) > 20000:
        raise SQLValidationError("SQL为空或超出长度上限")
    try:
        statements = parse(sql, read="mysql")
    except SqlglotError as exc:
        raise SQLValidationError("SQL语法无法解析") from exc
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
        raise SQLValidationError("仅允许单条SELECT/WITH查询")

    expression = statements[0]
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Merge, exp.Lock)
    if any(expression.find(node_type) is not None for node_type in forbidden):
        raise SQLValidationError("查询包含写入或结构变更操作")

    join_count = sum(1 for node in expression.walk() if isinstance(node, exp.Join))
    if join_count > max_joins:
        raise SQLValidationError(f"查询包含 {join_count} 个 JOIN，超出 {max_joins} 个上限")

    nodes = list(expression.walk())
    if len(nodes) > 600 or any(node.depth > 40 for node in nodes):
        raise SQLValidationError("查询复杂度超出上限")
    if any(node.comments for node in nodes) or any(cte.args.get("recursive") for cte in expression.find_all(exp.With)):
        raise SQLValidationError("不允许SQL注释或递归CTE")
    for node in nodes:
        if isinstance(node, exp.Func):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            if name not in _SAFE_FUNCTIONS:
                raise SQLValidationError("查询包含未授权的数据库函数")
        elif node.key not in _SAFE_NODES:
            raise SQLValidationError("查询包含未支持的SQL语法")
        if isinstance(node, (exp.Placeholder, exp.Parameter)):
            raise SQLValidationError("不支持未绑定参数")
        if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
            raise SQLValidationError("禁止SELECT *，请指定业务字段")
        if isinstance(node, exp.Table) and (node.db or node.catalog):
            raise SQLValidationError("不允许跨数据库访问")
        if isinstance(node, exp.Join):
            on = node.args.get("on")
            if node.args.get("kind") == "CROSS" or on is None or not list(on.find_all(exp.Column)):
                raise SQLValidationError("禁止笛卡尔积JOIN")
            predicates = list(on.flatten()) if isinstance(on, exp.And) else [on]
            if any(
                not isinstance(predicate, exp.EQ)
                or not isinstance(predicate.left, exp.Column)
                or not isinstance(predicate.right, exp.Column)
                or not predicate.left.table
                or predicate.left.table.lower() == predicate.right.table.lower()
                for predicate in predicates
            ):
                raise SQLValidationError("JOIN必须使用不同来源字段的等值条件，禁止恒真或OR条件")
    try:
        # 按作用域解析CTE/别名，不能把全部CTE名称从所有物理表中直接相减。
        for scope in traverse_scope(expression):
            for _, source in scope.selected_sources.values():
                if isinstance(source, exp.Table) and source.name.lower() not in allowed_tables:
                    raise SQLValidationError("查询访问了未授权的数据表")
        qualified = qualify(
            expression.copy(), dialect="mysql", schema={"orders": {name: "UNKNOWN" for name in ORDER_COLUMNS}},
            expand_stars=False, quote_identifiers=False, identify=False,
            validate_qualify_columns=True,
        )
    except (OptimizeError, SqlglotError) as exc:
        raise SQLValidationError("无法证明表或字段访问安全") from exc

    for column in qualified.find_all(exp.Column):
        if column.name.lower() not in IDENTIFIER_COLUMNS:
            continue
        parent = column.parent
        count_distinct = (
            isinstance(parent, exp.Distinct) and len(parent.expressions) == 1
            and isinstance(parent.parent, exp.Count)
        )
        # 复购用户数：内部按用户分组只输出次数，禁止在任何层投影用户标识。
        internal_group = isinstance(parent, exp.Group) and parent.parent is not qualified
        if not count_distinct and not internal_group:
            raise SQLValidationError("敏感标识字段仅允许去重计数或内部行为分组")

    for clause in expression.find_all(exp.Limit, exp.Offset):
        value = clause.expression
        if not isinstance(value, exp.Literal) or not value.is_int or int(value.this) < 0:
            raise SQLValidationError("LIMIT/OFFSET必须为非负整数字面量")
        if isinstance(clause, exp.Offset) and int(value.this) > 10000:
            raise SQLValidationError("OFFSET超出10000行上限")
        if isinstance(clause, exp.Limit) and int(value.this) > max_rows:
            clause.set("expression", exp.Literal.number(max_rows))

    limit = expression.args.get("limit")
    if limit is None:
        expression = expression.limit(max_rows)
    else:
        value = limit.expression
        if isinstance(value, exp.Literal) and value.is_int and int(value.this) > max_rows:
            expression.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return expression.sql(dialect="mysql")


def apply_execution_guard(sql: str, timeout_ms: int, dialect: str = "mysql") -> str:
    """把执行时限下推到数据库，返回改写后的 SQL。

    asyncio.wait_for 只能取消协程，无法中断已经在数据库内运行的查询：超时后
    API 已经返回"查询执行超时"，但慢查询仍在库上继续跑，连接也一直被占用，
    因此真正的上限必须由数据库自己执行。

    MySQL 用 MAX_EXECUTION_TIME 优化器提示（仅对 SELECT 生效）；
    SQLite / 其他方言没有等价语法，保持原样并依赖上层等待作为兜底。
    """
    if timeout_ms <= 0 or dialect != "mysql":
        return sql
    try:
        expression = parse_one(sql, read=dialect)
    except ParseError:
        return sql
    # MySQL 要求 hint 位于整条语句的第一个 SELECT 后；UNION 根节点本身
    # 不能挂 hint，因此定位左侧第一个 SELECT，使时限覆盖整条 UNION。
    target = expression if isinstance(expression, exp.Select) else next(expression.find_all(exp.Select), None)
    if target is None:
        return sql
    target.set(
        "hint",
        exp.Hint(
            expressions=[
                exp.Anonymous(this=_MYSQL_TIMEOUT_HINT, expressions=[exp.Literal.number(timeout_ms)])
            ]
        ),
    )
    return expression.sql(dialect=dialect)


def dialect_from_url(url: str) -> str:
    """从 SQLAlchemy 连接串提取方言名，如 mysql+pymysql://... -> mysql。"""
    return url.split("://", 1)[0].split("+", 1)[0].strip().lower()
