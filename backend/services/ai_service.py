import logging
import re
import json
import asyncio
import hashlib
from typing import Optional
from functools import lru_cache

from langchain_community.utilities.sql_database import SQLDatabase
from langchain_openai import ChatOpenAI
from langchain_community.agent_toolkits import create_sql_agent, SQLDatabaseToolkit

from backend.config import get_settings
from backend.models.schemas import AIQueryResponse
from backend.utils.text_cleaner import clean_sql as _clean_sql

logger = logging.getLogger(__name__)

settings = get_settings()

SENSITIVE_PATTERNS = [
    r"密码", r"手机号", r"身份证", r"地址.*具体",
    r"个人.*信息", r"隐私", r"password", r"phone.*number",
]

BUSINESS_CONTEXT = """## 数据时间范围
- 数据时间范围：2025-01-01 至 2025-12-31（共1年数据）
- 当用户提到"最近N天"时，指的是数据中的最近N天

## 业务指标定义
- 付款金额 = 实际销售额（非订单金额）
- 平台类型枚举值：APP、微信公众号、Web网站、其他
- 是否退款：是=已退款，否=未退款
- 退款率 = 退款订单数 / 总订单数
- 复购率 = 消费2次及以上的用户数 / 总用户数
- 客单价 = 总付款金额 / 总订单数

## 回答规则
1. 始终先查看表结构确认列名，再编写 SQL
2. 日期筛选使用 order_time 列，格式 'YYYY-MM-DD'
3. 金额查询使用 payment_amount
4. 退款相关使用 is_refunded = '是'
5. SQL 结果较大时使用 LIMIT 限制
6. 先给出数据结论，再附上 SQL 语句
7. 用中文回答
8. 仅回答电商数据相关问题"""


def _is_sensitive(query: str) -> bool:
    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, query, re.IGNORECASE):
            return True
    return False


def _extract_sql_from_intermediate(response: dict) -> Optional[str]:
    steps = response.get("intermediate_steps", [])
    for step in steps:
        if isinstance(step, tuple) and len(step) >= 2:
            action, observation = step
            if hasattr(action, "tool_input"):
                tool_input = action.tool_input
                if isinstance(tool_input, dict):
                    sql = tool_input.get("sql") or tool_input.get("query")
                    if sql and isinstance(sql, str) and "SELECT" in sql.upper():
                        return _clean_sql(sql, strip_html=True)
                elif isinstance(tool_input, str) and "SELECT" in tool_input.upper():
                    return _clean_sql(tool_input, strip_html=True)
            if isinstance(observation, str):
                clean_obs = re.sub(r'<[^>]+>', '', observation)
                sql_match = re.search(r'SELECT\s+[\s\S]+?(?:;|$)', clean_obs, re.IGNORECASE)
                if sql_match:
                    return _clean_sql(sql_match.group(0), strip_html=True)
    output = response.get("output", "")
    if isinstance(output, str):
        match = re.search(r'```sql\s*(.*?)```', output, re.IGNORECASE | re.DOTALL)
        if match:
            return _clean_sql(match.group(1), strip_html=True)
    return None


def _extract_sql_from_answer(answer: str) -> Optional[str]:
    if not answer or not isinstance(answer, str):
        return None
    # 保护代码块后再剥离 HTML 高亮标签（LLM 可能回显带 span 的 SQL）
    codeblocks: dict[str, str] = {}
    def _stash(m):
        key = f"\x00SQLCB{len(codeblocks)}ENDSQLCB\x00"
        codeblocks[key] = m.group(0)
        return key
    stripped = re.sub(r'```[\s\S]*?```', _stash, answer)
    stripped = re.sub(r'<[^>]+>', '', stripped)
    for k, v in codeblocks.items():
        stripped = stripped.replace(k, v)
    patterns = [
        r'```sql\s*(.*?)```',
        r'```(SELECT[\s\S]*?)```',
        r'(SELECT\s+[\s\S]*?;)',
    ]
    for pattern in patterns:
        match = re.search(pattern, stripped, re.IGNORECASE | re.DOTALL)
        if match:
            return _clean_sql(match.group(1), strip_html=True)
    return None


def _detect_chart_type(columns: list[str], rows: list) -> Optional[str]:
    if not rows or len(columns) < 2:
        return None
    col1 = columns[0].lower()
    time_keywords = ['date', '时间', 'time', 'hour', '日期', '月']
    if any(k in col1 for k in time_keywords):
        return "line"
    if len(rows) <= 6:
        return "pie"
    return "bar"


def _build_visualization(columns: list[str], rows: list) -> Optional[dict]:
    chart_type = _detect_chart_type(columns, rows)
    if not chart_type:
        return None
    return {
        "chart_type": chart_type,
        "x_field": columns[0],
        "y_field": columns[1] if len(columns) > 1 else None,
        "data": [dict(zip(columns, row)) for row in rows] if rows else [],
    }


_SQL_DESTRUCTIVE_KEYWORDS = (
    "DROP ", "DELETE ", "UPDATE ", "INSERT ", "ALTER ", "TRUNCATE ",
    "CREATE ", "GRANT ", "REVOKE ", "RENAME ",
)

# 用于剥除 SQL 注释与字符串字面量，避免 `/*DROP*/` 这类绕过
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQL_STRING_LITERAL = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"")


def _strip_sql_noise(sql: str) -> str:
    """移除 SQL 注释与字符串字面量，避免子串匹配被绕过。"""
    sql = _SQL_BLOCK_COMMENT.sub(" ", sql)
    sql = _SQL_LINE_COMMENT.sub(" ", sql)
    sql = _SQL_STRING_LITERAL.sub("''", sql)
    return sql


def _is_read_only_sql(sql: str) -> bool:
    """粗粒度只读检查：移除注释/字符串后判断是否含破坏性关键字。

    说明：这是 best-effort 防御层，真正的安全应：
      1) 数据库账号只授予 SELECT 权限；
      2) 上游 langchain SQLDatabaseToolkit 默认只暴露 sql_db_query 工具；
      3) 生产环境接入 SQL 解析器（sqlparse/sqlglot）做 AST 级别校验。
    """
    if not sql or not sql.strip():
        return False
    cleaned = _strip_sql_noise(sql).upper().strip()
    for kw in _SQL_DESTRUCTIVE_KEYWORDS:
        if kw in cleaned:
            return False
    return True


@lru_cache(maxsize=1)
def _get_sync_db() -> SQLDatabase:
    db_url = (
        f"mysql+pymysql://{settings.db_user}:{settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/{settings.db_name}?charset=utf8mb4"
    )
    return SQLDatabase.from_uri(
        db_url,
        engine_args={
            "pool_pre_ping": True,
            "pool_size": 3,
            "max_overflow": 2,
            "pool_recycle": 3600,
        },
    )


_agent_cache: dict = {"agent": None, "settings_hash": None}


def _get_settings_hash() -> str:
    return hashlib.md5(
        f"{settings.llm_api_key}:{settings.llm_base_url}:{settings.llm_model}".encode()
    ).hexdigest()


def _get_agent():
    if not settings.llm_api_key:
        return None

    current_hash = _get_settings_hash()
    if _agent_cache["agent"] is not None and _agent_cache["settings_hash"] == current_hash:
        return _agent_cache["agent"]

    db = _get_sync_db()
    llm = ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=0.1,
        timeout=120,
        max_retries=2,
    )
    toolkit = SQLDatabaseToolkit(db=db, llm=llm)

    prefix = f"""你是一个专业的 AI 智能商业分析助手。你可以访问一个名为 `orders` 的电商订单数据库表。

{BUSINESS_CONTEXT}

重要：你必须严格按照以下 ReAct 格式回答，每一步都要以 "Thought:" 开头：

Thought: 我需要查看表结构
Action: sql_db_schema
Action Input: orders

Thought: 根据表结构，我需要查询...
Action: sql_db_query
Action Input: SELECT ... FROM orders WHERE ...

Thought: 查询完成，总结结果
Final Answer: 根据查询结果...

注意：
- Action Input 必须是纯文本，不是 JSON
- sql_db_query 的 Action Input 必须是完整的 SELECT 语句
- 不要创建表，直接查询 orders 表
- 使用 payment_amount 表示付款金额（实际销售额）"""

    agent = create_sql_agent(
        llm=llm,
        toolkit=toolkit,
        prefix=prefix,
        verbose=False,
        agent_type="zero-shot-react-description",
        handle_parsing_errors=True,
    )
    _agent_cache["agent"] = agent
    _agent_cache["settings_hash"] = current_hash
    return agent


async def process_natural_language_query(query: str) -> AIQueryResponse:
    """处理自然语言查询，返回SQL执行结果"""
    if _is_sensitive(query):
        return AIQueryResponse(
            sql=None,
            result=[],
            answer="⚠️ 该数据已脱敏，仅支持聚合查询，无法提供用户个人隐私数据。",
            visualization=None,
        )

    if not settings.llm_api_key:
        return AIQueryResponse(
            sql=None,
            result=[],
            answer="⚠️ AI功能未配置，请在 .env 中设置 LLM_API_KEY。",
            visualization=None,
        )

    try:
        agent = _get_agent()
        if agent is None:
            return AIQueryResponse(
                sql=None,
                result=[],
                answer="⚠️ AI功能未配置，请在 .env 中设置 LLM_API_KEY。",
                visualization=None,
            )

        try:
            response = await asyncio.to_thread(agent.invoke, {"input": query})
        except Exception as invoke_err:
            err_msg = str(invoke_err)
            if "output parsing error" in err_msg.lower() or "Could not parse" in err_msg:
                match = re.search(r'Could not parse LLM output:\s*`([^`]*)', err_msg, re.DOTALL)
                raw_output = match.group(1).strip() if match else err_msg
                return AIQueryResponse(
                    sql=_extract_sql_from_answer(raw_output),
                    result=[],
                    answer=raw_output,
                    visualization=None,
                )
            raise

        answer = response.get("output", "抱歉，暂时无法回答这个问题。")

        if "output parsing error" in str(answer).lower() or "Could not parse" in str(answer):
            extracted_sql = _extract_sql_from_intermediate(response)
            if not extracted_sql:
                extracted_sql = _extract_sql_from_answer(answer)
            match = re.search(r'Could not parse LLM output:\s*`([^`]*)', answer, re.DOTALL)
            clean_answer = match.group(1).strip() if match else answer.split("Could not parse")[0].strip()

            result_data = []
            visualization = None
            if extracted_sql:
                try:
                    if _is_read_only_sql(extracted_sql):
                        db = _get_sync_db()
                        raw_result = await asyncio.to_thread(db.run, extracted_sql)
                        logger.info(f"SQL执行原始结果: {raw_result[:500] if isinstance(raw_result, str) else str(raw_result)[:500]}")
                        if isinstance(raw_result, str):
                            try:
                                parsed = json.loads(raw_result)
                                if isinstance(parsed, list):
                                    result_data = parsed
                            except json.JSONDecodeError:
                                import ast
                                from decimal import Decimal
                                try:
                                    import re as _re
                                    cleaned = _re.sub(r"Decimal\('([^']+)'\)", r"\1", raw_result)
                                    parsed = ast.literal_eval(cleaned)
                                    if isinstance(parsed, list):
                                        if parsed and isinstance(parsed[0], tuple):
                                            col_match = re.search(r'SELECT\s+(.+?)\s+FROM', extracted_sql, re.IGNORECASE)
                                            if col_match:
                                                cols = [c.strip().split(' AS ')[-1].strip('`"\'') for c in col_match.group(1).split(',')]
                                                result_data = [dict(zip(cols, row)) for row in parsed]
                                            else:
                                                result_data = [dict(enumerate(row)) for row in parsed]
                                        elif parsed and isinstance(parsed[0], dict):
                                            result_data = parsed
                                except (ValueError, SyntaxError):
                                    pass
                        elif isinstance(raw_result, list):
                            if raw_result and isinstance(raw_result[0], dict):
                                result_data = raw_result
                            elif raw_result and isinstance(raw_result[0], tuple):
                                col_match = re.search(r'SELECT\s+(.+?)\s+FROM', extracted_sql, re.IGNORECASE)
                                if col_match:
                                    cols = [c.strip().split(' AS ')[-1].strip('`"\'') for c in col_match.group(1).split(',')]
                                    result_data = [dict(zip(cols, row)) for row in raw_result]

                        if result_data and isinstance(result_data[0], dict):
                            columns = list(result_data[0].keys())
                            rows = [list(item.values()) for item in result_data]
                            visualization = _build_visualization(columns, rows)
                except Exception as e:
                    logger.warning(f"解析错误后SQL执行失败: {e}")

            if not result_data:
                steps = response.get("intermediate_steps", [])
                for step in steps:
                    if isinstance(step, tuple) and len(step) >= 2:
                        _, observation = step
                        if isinstance(observation, str):
                            obs_match = re.search(r'\[?\{[^}]+\}?\]', observation)
                            if obs_match:
                                try:
                                    parsed = json.loads(obs_match.group(0))
                                    if isinstance(parsed, list):
                                        result_data = parsed
                                    elif isinstance(parsed, dict):
                                        result_data = [parsed]
                                    if result_data and isinstance(result_data[0], dict):
                                        columns = list(result_data[0].keys())
                                        rows = [list(item.values()) for item in result_data]
                                        visualization = _build_visualization(columns, rows)
                                    break
                                except json.JSONDecodeError:
                                    pass

            return AIQueryResponse(
                sql=extracted_sql,
                result=result_data,
                answer=clean_answer,
                visualization=visualization,
            )

        extracted_sql = _extract_sql_from_intermediate(response)

        result_data = []
        visualization = None

        if extracted_sql:
            try:
                if not _is_read_only_sql(extracted_sql):
                    logger.warning(f"拦截危险SQL: {extracted_sql[:100]}")
                    return AIQueryResponse(
                        sql=None,
                        result=[],
                        answer="⚠️ 仅支持数据查询操作，不允许修改数据库",
                        visualization=None,
                    )
                db = _get_sync_db()
                raw_result = await asyncio.to_thread(db.run, extracted_sql)
                logger.info(f"SQL执行原始结果: {raw_result[:500] if isinstance(raw_result, str) else str(raw_result)[:500]}")
                if isinstance(raw_result, str):
                    try:
                        parsed = json.loads(raw_result)
                        if isinstance(parsed, list):
                            result_data = parsed
                    except json.JSONDecodeError:
                        import ast
                        from decimal import Decimal
                        try:
                            # 替换 Decimal(...) 为直接数值
                            import re as _re
                            cleaned = _re.sub(r"Decimal\('([^']+)'\)", r"\1", raw_result)
                            parsed = ast.literal_eval(cleaned)
                            if isinstance(parsed, list):
                                if parsed and isinstance(parsed[0], tuple):
                                    col_match = re.search(r'SELECT\s+(.+?)\s+FROM', extracted_sql, re.IGNORECASE)
                                    if col_match:
                                        cols = [c.strip().split(' AS ')[-1].strip('`"\'') for c in col_match.group(1).split(',')]
                                        result_data = [dict(zip(cols, row)) for row in parsed]
                                    else:
                                        result_data = [dict(enumerate(row)) for row in parsed]
                                elif parsed and isinstance(parsed[0], dict):
                                    result_data = parsed
                            elif isinstance(parsed, dict):
                                result_data = [parsed]
                            elif isinstance(parsed, tuple):
                                if parsed and isinstance(parsed[0], tuple):
                                    col_match = re.search(r'SELECT\s+(.+?)\s+FROM', extracted_sql, re.IGNORECASE)
                                    if col_match:
                                        cols = [c.strip().split(' AS ')[-1].strip('`"\'') for c in col_match.group(1).split(',')]
                                        result_data = [dict(zip(cols, parsed[0]))]
                        except (ValueError, SyntaxError):
                            pass
                elif isinstance(raw_result, list):
                    if raw_result and isinstance(raw_result[0], dict):
                        result_data = raw_result
                    elif raw_result and isinstance(raw_result[0], tuple):
                        col_match = re.search(r'SELECT\s+(.+?)\s+FROM', extracted_sql, re.IGNORECASE)
                        if col_match:
                            cols = [c.strip().split(' AS ')[-1].strip('`"\'') for c in col_match.group(1).split(',')]
                            result_data = [dict(zip(cols, row)) for row in raw_result]

                if result_data and isinstance(result_data[0], dict):
                    columns = list(result_data[0].keys())
                    rows = [list(item.values()) for item in result_data]
                    visualization = _build_visualization(columns, rows)
            except Exception as e:
                logger.warning(f"SQL执行失败: {e}")

        return AIQueryResponse(
            sql=extracted_sql,
            result=result_data,
            answer=answer,
            visualization=visualization,
        )

    except Exception as e:
        logger.error(f"AI查询处理失败: {e}")
        return AIQueryResponse(
            sql=None,
            result=[],
            answer="⚠️ 查询处理失败，请稍后重试",
            visualization=None,
        )
